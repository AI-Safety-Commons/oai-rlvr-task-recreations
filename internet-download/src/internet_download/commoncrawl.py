from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from .http import atomic_write, request

DATA_ROOT = "https://data.commoncrawl.org"
DEFAULT_CONTACT_EMAIL = "concurrentsquared@concurrentsquared.com"
COMMON_CRAWL_RETRY_STATUSES = (403, 429, 500, 502, 503, 504)


def common_crawl_request(
    url: str,
    *,
    contact_email: str = DEFAULT_CONTACT_EMAIL,
    headers: dict[str, str] | None = None,
):
    """Make an identifiable, conservatively retried Common Crawl request."""
    identity_headers = {**common_crawl_headers(contact_email), **(headers or {})}
    return request(
        url,
        headers=identity_headers,
        attempts=8,
        retry_statuses=COMMON_CRAWL_RETRY_STATUSES,
    )


def common_crawl_headers(contact_email: str) -> dict[str, str]:
    if not contact_email or "\r" in contact_email or "\n" in contact_email:
        raise ValueError("Common Crawl contact_email must be a valid mailbox")
    return {
        "From": contact_email,
        "User-Agent": (
            "internet-download/0.1 "
            f"(offline evaluation corpus builder; {contact_email})"
        ),
    }


class RequestPacer:
    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(0.0, interval_seconds)
        self.next_request = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        # Serialize the calculation as well as the sleep so concurrent download
        # workers cannot all pass the rate limiter at the same instant.
        with self._lock:
            now = time.monotonic()
            if self.next_request > now:
                time.sleep(self.next_request - now)
            self.next_request = time.monotonic() + self.interval_seconds


@dataclass(frozen=True)
class Capture:
    url: str
    timestamp: str
    filename: str
    offset: int
    length: int
    digest: str
    mime: str
    status: str

    @property
    def identity(self) -> str:
        value = self.digest or f"{self.url}\0{self.timestamp}"
        return hashlib.sha256(value.encode()).hexdigest()


def columnar_index_paths(
    crawl: str,
    cache_dir: Path,
    *,
    contact_email: str = DEFAULT_CONTACT_EMAIL,
) -> list[str]:
    """Return the crawl's URL Index Parquet shards, caching its small manifest."""
    manifest = cache_dir / "cc-index-table.paths.gz"
    if not manifest.exists():
        url = f"{DATA_ROOT}/crawl-data/{crawl}/cc-index-table.paths.gz"
        with common_crawl_request(url, contact_email=contact_email) as response:
            atomic_write(manifest, response.read())
    try:
        lines = gzip.decompress(manifest.read_bytes()).decode().splitlines()
    except (gzip.BadGzipFile, UnicodeDecodeError) as error:
        manifest.unlink(missing_ok=True)
        raise ValueError(
            f"invalid Common Crawl URL Index manifest for {crawl}"
        ) from error
    paths = [f"{DATA_ROOT}/{line}" for line in lines if line.strip()]
    if not paths:
        raise ValueError(f"empty Common Crawl URL Index manifest for {crawl}")
    return paths


def _target_row(ordinal: int, target: dict[str, Any]) -> tuple[Any, ...]:
    pattern = str(target["url"])
    match_type = str(target.get("match_type", "domain"))
    if match_type == "domain":
        host = pattern.removesuffix("/*").rstrip("/").lower()
        if "://" in host:
            host = urllib.parse.urlsplit(host).hostname or ""
        else:
            host = host.split("/", 1)[0]
    elif match_type in {"exact", "prefix"}:
        host = urllib.parse.urlsplit(pattern).hostname or ""
    else:
        raise ValueError(f"unsupported Common Crawl match_type: {match_type}")
    if not host:
        raise ValueError(f"Common Crawl target has no host: {pattern}")
    return (
        ordinal,
        pattern,
        match_type,
        host.lower(),
        ".".join(reversed(host.lower().split("."))),
        host.rsplit(".", 1)[-1].lower(),
        int(target.get("max_records", 10_000)),
    )


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def discover_columnar(
    paths: list[str],
    targets: list[dict[str, Any]],
    *,
    database: Path | str = ":memory:",
    threads: int = 4,
    contact_email: str = DEFAULT_CONTACT_EMAIL,
) -> Iterator[Capture]:
    """Query all planning targets in one DuckDB scan of the Parquet URL Index."""
    if not paths or not targets:
        return
    target_rows = [_target_row(index, target) for index, target in enumerate(targets)]
    if database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    remote = any(path.startswith(("http://", "https://", "s3://")) for path in paths)
    headers = common_crawl_headers(contact_email)
    connect_config = {"custom_user_agent": headers["User-Agent"]} if remote else {}
    connection = duckdb.connect(str(database), config=connect_config)
    try:
        connection.execute("SET threads = ?", [max(1, threads)])
        connection.execute("SET preserve_insertion_order = false")
        if remote:
            connection.execute("SET http_retries = 8")
            connection.execute("SET http_retry_wait_ms = 1000")
            connection.execute("SET http_retry_backoff = 2")
            connection.execute(
                "CREATE OR REPLACE SECRET commoncrawl_http ("
                "TYPE HTTP, SCOPE 'https://data.commoncrawl.org/', "
                "EXTRA_HTTP_HEADERS MAP {"
                f"'From': {_sql_string(headers['From'])}, "
                f"'User-Agent': {_sql_string(headers['User-Agent'])}"
                "})"
            )
        connection.execute(
            """
            CREATE OR REPLACE TEMP TABLE planning_targets (
                target_id INTEGER,
                pattern VARCHAR,
                match_type VARCHAR,
                host VARCHAR,
                reversed_host VARCHAR,
                tld VARCHAR,
                max_records INTEGER
            )
            """
        )
        connection.executemany(
            "INSERT INTO planning_targets VALUES (?, ?, ?, ?, ?, ?, ?)", target_rows
        )
        cursor = connection.execute(
            """
            WITH matched AS (
                SELECT
                    t.target_id,
                    t.max_records,
                    i.url,
                    strftime(i.fetch_time, '%Y%m%d%H%M%S') AS timestamp,
                    i.warc_filename AS filename,
                    i.warc_record_offset AS warc_offset,
                    i.warc_record_length AS warc_length,
                    coalesce(i.content_digest, '') AS digest,
                    coalesce(i.content_mime_detected, i.content_mime_type, '') AS mime,
                    cast(i.fetch_status AS VARCHAR) AS status
                FROM read_parquet(?) AS i
                JOIN planning_targets AS t
                  ON i.url_host_tld = t.tld
                 AND (
                     i.url_host_name_reversed = t.reversed_host
                     OR starts_with(
                         i.url_host_name_reversed, t.reversed_host || '.'
                     )
                 )
                 AND CASE t.match_type
                     WHEN 'domain' THEN true
                     WHEN 'exact' THEN i.url = t.pattern
                     WHEN 'prefix' THEN starts_with(i.url, t.pattern)
                     ELSE false
                 END
                WHERE i.fetch_status = 200
                  AND i.warc_filename IS NOT NULL
            ), deduplicated AS (
                SELECT *
                FROM matched
                QUALIFY row_number() OVER (
                    PARTITION BY target_id, url
                    ORDER BY timestamp DESC, digest
                ) = 1
            ), limited AS (
                SELECT *
                FROM deduplicated
                QUALIFY row_number() OVER (
                    PARTITION BY target_id ORDER BY url, timestamp DESC
                ) <= max_records
            )
            SELECT url, timestamp, filename, warc_offset, warc_length,
                   digest, mime, status
            FROM limited
            ORDER BY row_number() OVER (
                PARTITION BY target_id ORDER BY url, timestamp DESC
            ), target_id
            """,
            [paths],
        )
        while rows := cursor.fetchmany(10_000):
            for row in rows:
                yield Capture(*row)
    finally:
        connection.close()


def write_plan(captures: Iterable[Capture], path: Path) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    unique: dict[str, Capture] = {}
    for capture in captures:
        unique.setdefault(capture.identity, capture)
    # Preserve discovery order. The CLI interleaves domains so a global byte cap
    # cannot be consumed entirely by alphabetically early or unusually large sites.
    ordered = list(unique.values())
    body = "".join(
        json.dumps(capture.__dict__, sort_keys=True) + "\n" for capture in ordered
    ).encode()
    atomic_write(path, body)
    return len(ordered), sum(capture.length for capture in ordered)


def read_plan(path: Path) -> list[Capture]:
    return [
        Capture(**json.loads(line)) for line in path.read_text().splitlines() if line
    ]


def _download_one(
    capture: Capture,
    records: Path,
    pacer: RequestPacer,
    contact_email: str,
) -> dict[str, Any]:
    destination = records / capture.identity[:2] / f"{capture.identity}.warc.gz"
    if not destination.exists():
        start = capture.offset
        end = start + capture.length - 1
        try:
            pacer.wait()
            with common_crawl_request(
                f"{DATA_ROOT}/{capture.filename}",
                contact_email=contact_email,
                headers={"Range": f"bytes={start}-{end}"},
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            if error.code in {403, 404, 416, 429, 500, 502, 503, 504}:
                return {"skipped": True, "url": capture.url, "status": error.code}
            raise
        if len(body) != capture.length:
            raise ValueError(
                f"range length mismatch for {capture.url}: "
                f"expected {capture.length}, received {len(body)}"
            )
        atomic_write(destination, body)
    return {
        **capture.__dict__,
        "local_path": destination.relative_to(records.parent).as_posix(),
        "bytes": destination.stat().st_size,
    }


def download_plan(
    plan: Path,
    output: Path,
    *,
    workers: int = 8,
    max_total_bytes: int | None = None,
    request_interval_seconds: float = 1.0,
    request_rate_per_second: float | None = None,
    contact_email: str = DEFAULT_CONTACT_EMAIL,
) -> tuple[int, int]:
    captures = read_plan(plan)
    selected: list[Capture] = []
    total = 0
    for capture in captures:
        if max_total_bytes is not None and total + capture.length > max_total_bytes:
            break
        selected.append(capture)
        total += capture.length

    records = output / "records"
    if request_rate_per_second is not None:
        if request_rate_per_second <= 0:
            raise ValueError("request_rate_per_second must be positive")
        request_interval_seconds = 1.0 / request_rate_per_second
    pacer = RequestPacer(request_interval_seconds)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(_download_one, item, records, pacer, contact_email)
            for item in selected
        ]
        results = []
        for completed, future in enumerate(as_completed(futures), 1):
            row = future.result()
            results.append(row)
            if row.get("skipped"):
                print(
                    f"[{completed:,}/{len(selected):,}] skipped capture "
                    f"HTTP {row['status']}: {row['url']}",
                    flush=True,
                )
            else:
                print(
                    f"[{completed:,}/{len(selected):,}] downloaded "
                    f"{row['bytes']:,} bytes: {row['url']}",
                    flush=True,
                )
    skipped = sum(1 for row in results if row.get("skipped"))
    results = [row for row in results if not row.get("skipped")]
    if skipped:
        print(f"Warning: skipped {skipped:,} forbidden/missing WARC captures")
    manifest = output / "manifest.jsonl"
    atomic_write(
        manifest,
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in results).encode(),
    )
    return len(results), sum(row["bytes"] for row in results)
