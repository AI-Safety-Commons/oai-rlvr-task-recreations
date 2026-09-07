from __future__ import annotations

import hashlib
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .http import atomic_write, request

RANGE_CHUNK_SIZE = 64 * 1024 * 1024
CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


def archive_filename(archive: dict[str, Any]) -> str:
    parsed = urllib.parse.urlparse(archive["url"])
    filename = Path(parsed.path).name
    if not filename.endswith(".zim"):
        raise ValueError(f"Kiwix URL does not name a .zim file: {archive['url']}")
    return filename


def _download_sequential(url: str, partial: Path) -> None:
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with request(url, headers=headers, timeout=300) as response:
        status = getattr(response, "status", response.getcode())
        mode = "ab" if offset and status == 206 else "wb"
        with partial.open(mode) as target:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)


def _range_size(url: str) -> int | None:
    with request(url, headers={"Range": "bytes=0-0"}, timeout=300) as response:
        status = getattr(response, "status", response.getcode())
        if status != 206:
            return None
        content_range = response.headers.get("Content-Range", "")
        match = CONTENT_RANGE.fullmatch(content_range.strip())
        if not match or match.group(1, 2) != ("0", "0"):
            return None
        response.read(1)
        return int(match.group(3))


def _download_range(url: str, partial: Path, start: int, end: int) -> None:
    with request(
        url, headers={"Range": f"bytes={start}-{end}"}, timeout=300
    ) as response:
        status = getattr(response, "status", response.getcode())
        if status != 206:
            raise ValueError(f"Server ignored Kiwix byte range {start}-{end}")
        content_range = response.headers.get("Content-Range", "")
        match = CONTENT_RANGE.fullmatch(content_range.strip())
        if not match or tuple(map(int, match.group(1, 2))) != (start, end):
            raise ValueError(f"Server returned wrong Kiwix byte range: {content_range}")
        remaining = end - start + 1
        with partial.open("r+b") as target:
            target.seek(start)
            while remaining:
                chunk = response.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError(f"Short Kiwix byte range {start}-{end}")
                target.write(chunk)
                remaining -= len(chunk)


def _download_range_with_retries(
    url: str, partial: Path, start: int, end: int, attempts: int = 4
) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            _download_range(url, partial, start, end)
            return
        except (
            OSError,
            urllib.error.URLError,
            TimeoutError,
            http.client.HTTPException,
        ) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(min(2**attempt, 8))
    assert last_error is not None
    raise last_error


def _download_parallel(url: str, partial: Path, size: int, workers: int) -> None:
    metadata = partial.with_suffix(partial.suffix + ".json")
    plan: list[tuple[int, int]]
    completed: set[tuple[int, int]]

    if metadata.exists():
        state = json.loads(metadata.read_text())
        if state.get("url") != url or state.get("size") != size:
            raise ValueError(f"Stale parallel-download metadata: {metadata}")
        plan = [tuple(item) for item in state["ranges"]]
        completed = {tuple(item) for item in state["completed"]}
    else:
        prefix = min(partial.stat().st_size if partial.exists() else 0, size)
        plan = [
            (start, min(start + RANGE_CHUNK_SIZE, size) - 1)
            for start in range(prefix, size, RANGE_CHUNK_SIZE)
        ]
        completed = set()

    partial.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("ab") as target:
        target.truncate(size)

    lock = threading.Lock()

    def save_progress() -> None:
        state = {
            "url": url,
            "size": size,
            "ranges": [list(item) for item in plan],
            "completed": [list(item) for item in sorted(completed)],
        }
        atomic_write(metadata, json.dumps(state, sort_keys=True).encode("utf-8"))

    save_progress()
    pending = [item for item in plan if item not in completed]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_download_range_with_retries, url, partial, start, end): (
                start,
                end,
            )
            for start, end in pending
        }
        for future in as_completed(futures):
            future.result()
            with lock:
                completed.add(futures[future])
                save_progress()
    metadata.unlink(missing_ok=True)


def _verify_checksum(archive: dict[str, Any], destination: Path) -> None:
    expected = archive.get("sha256")
    if not expected:
        return

    digest = hashlib.sha256()
    with destination.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest().lower() != str(expected).lower():
        destination.rename(destination.with_suffix(destination.suffix + ".bad"))
        raise ValueError(f"SHA-256 mismatch for {destination.name}")


def download_archive(
    archive: dict[str, Any], output: Path, *, workers: int = 4
) -> Path:
    if workers < 1:
        raise ValueError("Kiwix workers must be at least 1")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / archive_filename(archive)
    if destination.exists() and not archive.get("sha256"):
        return destination
    partial = destination.with_suffix(destination.suffix + ".part")
    metadata = partial.with_suffix(partial.suffix + ".json")
    size = _range_size(archive["url"])
    if size is None:
        if metadata.exists():
            raise ValueError(
                "Cannot resume parallel Kiwix download without byte ranges"
            )
        _download_sequential(archive["url"], partial)
    else:
        _download_parallel(archive["url"], partial, size, workers)
    partial.replace(destination)
    _verify_checksum(archive, destination)
    return destination
