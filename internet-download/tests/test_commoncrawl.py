import gzip
import io
import urllib.error
from pathlib import Path

import duckdb

from internet_download.cli import commoncrawl_targets
from internet_download.commoncrawl import (
    COMMON_CRAWL_RETRY_STATUSES,
    Capture,
    RequestPacer,
    columnar_index_paths,
    common_crawl_request,
    discover_columnar,
    read_plan,
    write_plan,
)
from internet_download.fallbacks import (
    load_unavailable_routes,
    load_unavailable_sites,
    policy_for_host,
    policy_for_request,
    render_unavailable,
)
from internet_download.http import request as http_request
from internet_download.kiwix import (
    _download_range_with_retries,
    archive_filename,
    download_archive,
)
from internet_download.targets import load_targets, merge_targets


def capture(url: str, digest: str) -> Capture:
    return Capture(url, "20260101", "part.warc.gz", 10, 20, digest, "text/html", "200")


def test_plan_deduplicates_by_digest(tmp_path: Path) -> None:
    path = tmp_path / "plan.jsonl"
    count, size = write_plan(
        [capture("https://a.example", "same"), capture("https://b.example", "same")],
        path,
    )
    assert (count, size) == (1, 20)
    assert len(read_plan(path)) == 1


def test_kiwix_filename_rejects_non_zim() -> None:
    assert archive_filename({"url": "https://example.test/wiki.zim"}) == "wiki.zim"


def test_kiwix_parallel_download_resumes_existing_prefix(
    monkeypatch, tmp_path: Path
) -> None:
    body = b"abcdefghijklmnopqrstuvwxyz"
    output = tmp_path / "kiwix"
    output.mkdir()
    partial = output / "wiki.zim.part"
    partial.write_bytes(body[:5])
    observed_ranges = []

    class Response:
        status = 206

        def __init__(self, payload: bytes, content_range: str):
            self.stream = io.BytesIO(payload)
            self.headers = {"Content-Range": content_range}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def getcode(self):
            return self.status

        def read(self, size=-1):
            return self.stream.read(size)

    def fake_request(url, *, headers, timeout):
        assert url == "https://example.test/wiki.zim"
        assert timeout == 300
        byte_range = headers["Range"]
        observed_ranges.append(byte_range)
        if byte_range == "bytes=0-0":
            return Response(body[:1], f"bytes 0-0/{len(body)}")
        start, end = map(int, byte_range.removeprefix("bytes=").split("-"))
        return Response(body[start : end + 1], f"bytes {start}-{end}/{len(body)}")

    monkeypatch.setattr("internet_download.kiwix.request", fake_request)
    monkeypatch.setattr("internet_download.kiwix.RANGE_CHUNK_SIZE", 4)
    destination = download_archive(
        {"url": "https://example.test/wiki.zim"}, output, workers=4
    )

    assert destination.read_bytes() == body
    assert "bytes=0-4" not in observed_ranges
    assert set(observed_ranges[1:]) == {
        "bytes=5-8",
        "bytes=9-12",
        "bytes=13-16",
        "bytes=17-20",
        "bytes=21-24",
        "bytes=25-25",
    }
    assert not (output / "wiki.zim.part.json").exists()


def test_kiwix_range_retries_broken_connections(monkeypatch, tmp_path: Path) -> None:
    calls = 0

    def flaky_download(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BrokenPipeError("mirror closed the connection")

    monkeypatch.setattr("internet_download.kiwix._download_range", flaky_download)
    monkeypatch.setattr("internet_download.kiwix.time.sleep", lambda _: None)
    _download_range_with_retries("https://example.test/wiki.zim", tmp_path / "x", 0, 9)
    assert calls == 2


def test_request_pacer_accepts_zero_interval() -> None:
    pacer = RequestPacer(0)
    pacer.wait()
    pacer.wait()


def test_common_crawl_request_is_identifiable_and_retries_403(monkeypatch) -> None:
    observed = {}
    response = object()

    def fake_request(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return response

    monkeypatch.setattr("internet_download.commoncrawl.request", fake_request)
    result = common_crawl_request(
        "https://data.commoncrawl.org/example",
        contact_email="concurrentsquared@concurrentsquared.com",
        headers={"Range": "bytes=10-29"},
    )

    assert result is response
    assert observed["headers"]["From"] == "concurrentsquared@concurrentsquared.com"
    assert (
        "concurrentsquared@concurrentsquared.com" in observed["headers"]["User-Agent"]
    )
    assert observed["headers"]["Range"] == "bytes=10-29"
    assert observed["attempts"] == 8
    assert 403 in COMMON_CRAWL_RETRY_STATUSES


def test_http_request_backs_off_exponentially_for_403(monkeypatch) -> None:
    calls = 0
    sleeps = []

    class RawResponse:
        status = 200

        def __init__(self):
            self.headers = {}

        def read(self, _size=-1):
            return b""

        def release_conn(self):
            return None

    def fake_pool_request(method, url, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        return RawResponse()

    monkeypatch.setattr("internet_download.http.POOL.request", fake_pool_request)
    monkeypatch.setattr("internet_download.http.time.sleep", sleeps.append)

    result = http_request(
        "https://data.commoncrawl.org/example",
        attempts=3,
        retry_statuses={403},
    )

    assert result.status == 200
    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_columnar_manifest_is_downloaded_once_and_cached(monkeypatch, tmp_path) -> None:
    body = gzip.compress(
        b"cc-index/table/cc-main/warc/crawl=CC-MAIN-2026-17/"
        b"subset=warc/part-00000.parquet\n"
    )
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return body

    def fake_request(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(
        "internet_download.commoncrawl.common_crawl_request", fake_request
    )
    first = columnar_index_paths("CC-MAIN-2026-17", tmp_path)
    second = columnar_index_paths("CC-MAIN-2026-17", tmp_path)

    assert first == second
    assert first == [
        (
            "https://data.commoncrawl.org/cc-index/table/cc-main/warc/"
            "crawl=CC-MAIN-2026-17/subset=warc/part-00000.parquet"
        )
    ]
    assert len(calls) == 1


def test_duckdb_planner_matches_targets_and_limits_records(tmp_path: Path) -> None:
    parquet = tmp_path / "index.parquet"
    connection = duckdb.connect()
    connection.execute(
        """
        CREATE TABLE index_rows (
            url VARCHAR,
            url_host_name VARCHAR,
            url_host_name_reversed VARCHAR,
            url_host_tld VARCHAR,
            fetch_time TIMESTAMP,
            fetch_status SMALLINT,
            content_digest VARCHAR,
            content_mime_type VARCHAR,
            content_mime_detected VARCHAR,
            warc_filename VARCHAR,
            warc_record_offset BIGINT,
            warc_record_length BIGINT
        )
        """
    )
    connection.executemany(
        "INSERT INTO index_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "https://docs.example.com/a",
                "docs.example.com",
                "com.example.docs",
                "com",
                "2026-01-01 00:00:00",
                200,
                "A",
                "text/html",
                "text/html",
                "a.warc.gz",
                10,
                20,
            ),
            (
                "https://docs.example.com/a",
                "docs.example.com",
                "com.example.docs",
                "com",
                "2025-12-01 00:00:00",
                200,
                "OLDER-A",
                "text/html",
                "text/html",
                "older-a.warc.gz",
                11,
                21,
            ),
            (
                "https://example.com/b",
                "example.com",
                "com.example",
                "com",
                "2026-01-02 00:00:00",
                200,
                "B",
                "text/html",
                None,
                "b.warc.gz",
                30,
                40,
            ),
            (
                "https://api.test.org/v1/items/1",
                "api.test.org",
                "org.test.api",
                "org",
                "2026-01-03 00:00:00",
                200,
                "C",
                "application/json",
                "application/json",
                "c.warc.gz",
                50,
                60,
            ),
            (
                "https://only.test.net/page",
                "only.test.net",
                "net.test.only",
                "net",
                "2026-01-04 00:00:00",
                200,
                "D",
                "text/html",
                "text/html",
                "d.warc.gz",
                70,
                80,
            ),
            (
                "https://example.com/not-found",
                "example.com",
                "com.example",
                "com",
                "2026-01-05 00:00:00",
                404,
                "E",
                "text/html",
                "text/html",
                "e.warc.gz",
                90,
                100,
            ),
        ],
    )
    connection.execute(f"COPY index_rows TO '{parquet}' (FORMAT PARQUET)")
    connection.close()

    captures = list(
        discover_columnar(
            [str(parquet)],
            [
                {"url": "example.com", "match_type": "domain", "max_records": 1},
                {
                    "url": "https://api.test.org/v1/",
                    "match_type": "prefix",
                    "max_records": 5,
                },
                {
                    "url": "https://only.test.net/page",
                    "match_type": "exact",
                    "max_records": 5,
                },
            ],
            database=tmp_path / "planning.duckdb",
            threads=2,
        )
    )

    assert {capture.url for capture in captures} == {
        "https://docs.example.com/a",
        "https://api.test.org/v1/items/1",
        "https://only.test.net/page",
    }
    assert all(capture.status == "200" for capture in captures)
    docs_capture = next(
        capture for capture in captures if capture.url == "https://docs.example.com/a"
    )
    assert docs_capture.digest == "A"
    assert docs_capture.timestamp == "20260101000000"


def test_load_targets_skips_disabled_and_explicit_overrides(tmp_path: Path) -> None:
    path = tmp_path / "sites.csv"
    path.write_text(
        "rank,domain,category,max_records,enabled\n"
        "1,example.com,reference,100,true\n"
        "2,video.example,video,0,false\n"
    )
    ranked = load_targets(path)
    merged = merge_targets(ranked, [{"url": "example.com", "max_records": 500}])
    assert len(merged) == 1
    assert merged[0]["max_records"] == 500
    assert merged[0]["category"] == "reference"


def test_preview_cap_limits_every_planning_target(tmp_path: Path) -> None:
    sites = tmp_path / "sites.csv"
    sites.write_text(
        "rank,domain,category,max_records,enabled\n1,example.com,reference,100,true\n"
    )
    config = {
        "targets_file": "sites.csv",
        "max_records_per_target": 5,
        "targets": [{"url": "extra.org", "max_records": 50}],
    }

    targets = commoncrawl_targets(tmp_path / "config.toml", config)

    assert {target["url"] for target in targets} == {"example.com", "extra.org"}
    assert all(target["max_records"] == 5 for target in targets)


def test_unavailable_site_policy_matches_subdomain(tmp_path: Path) -> None:
    path = tmp_path / "unavailable.toml"
    path.write_text(
        '[sites."example.com"]\n'
        "status = 429\n"
        'title = "Slow down"\n'
        'message = "Automated requests were limited."\n'
        "retry_after = 60\n"
    )
    policy = policy_for_host("www.example.com:443", load_unavailable_sites(path))
    assert policy is not None
    response = render_unavailable(policy)
    assert response.status == 429
    assert response.headers["Retry-After"] == "60"
    assert b"Automated requests were limited" in response.body


def test_search_route_policy_requires_path_and_query(tmp_path: Path) -> None:
    path = tmp_path / "unavailable.toml"
    path.write_text(
        '[routes."search.example"]\n'
        'path_prefixes = ["/search"]\n'
        'query_parameters = ["q"]\n'
        "status = 429\n"
        'title = "Limited"\n'
        'message = "Try later."\n'
    )
    policies = load_unavailable_routes(path)
    assert policy_for_request("search.example", "/", {"q": ["x"]}, policies) is None
    assert policy_for_request("search.example", "/search", {}, policies) is None
    assert policy_for_request("search.example", "/search", {"q": ["x"]}, policies)
