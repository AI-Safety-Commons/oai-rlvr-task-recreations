from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
from collections.abc import Collection, Iterator
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Self

import urllib3

USER_AGENT = "internet-download/0.1 (+offline evaluation corpus builder)"
POOL = urllib3.PoolManager(num_pools=32, maxsize=16, block=True)


class PooledResponse:
    def __init__(self, response: urllib3.response.HTTPResponse) -> None:
        self.status = response.status
        self.headers = response.headers
        self._response = response

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self._response.release_conn()


def request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
    attempts: int = 4,
    retry_statuses: Collection[int] = (429, 500, 502, 503, 504),
) -> PooledResponse:
    combined = {"User-Agent": USER_AGENT, **(headers or {})}
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = POOL.request(
                "GET",
                url,
                headers=combined,
                preload_content=False,
                timeout=urllib3.Timeout(connect=timeout, read=timeout),
            )
            if response.status >= 400:
                error = urllib.error.HTTPError(
                    url,
                    response.status,
                    f"HTTP {response.status}",
                    response.headers,
                    response,
                )
                response.release_conn()
                raise error
            return PooledResponse(response)
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code not in retry_statuses or attempt + 1 >= attempts:
                raise
            retry_after = error.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = float(retry_after)
            elif retry_after:
                try:
                    delay = max(
                        0.0,
                        parsedate_to_datetime(retry_after).timestamp() - time.time(),
                    )
                except (TypeError, ValueError, OverflowError):
                    delay = float(2**attempt)
            else:
                delay = float(2**attempt)
            time.sleep(min(delay, 60.0))
        except (
            urllib.error.URLError,
            urllib3.exceptions.HTTPError,
            TimeoutError,
            http.client.RemoteDisconnected,
        ) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def iter_json_lines(response: Any) -> Iterator[dict[str, Any]]:
    for raw_line in response:
        line = raw_line.decode("utf-8").strip()
        if line:
            yield json.loads(line)


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(body)
    temporary.replace(path)
