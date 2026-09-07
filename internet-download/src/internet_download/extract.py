from __future__ import annotations

import gzip
import json
import re
import urllib.parse
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

WHITESPACE = re.compile(r"\s+")
TEXT_TYPES = ("application/json", "application/xml", "application/xhtml+xml", "text/")


def _headers(block: bytes) -> tuple[str, dict[str, str]]:
    lines = block.decode("latin-1", errors="replace").splitlines()
    first = lines[0] if lines else ""
    result: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            result[name.strip().lower()] = value.strip()
    return first, result


def read_warc_response(path: Path) -> tuple[dict[str, str], dict[str, str], bytes]:
    with gzip.open(path, "rb") as source:
        record = source.read()
    warc_block, separator, payload = record.partition(b"\r\n\r\n")
    if not separator:
        warc_block, separator, payload = record.partition(b"\n\n")
    if not separator:
        raise ValueError(f"invalid WARC headers: {path}")
    _, warc_headers = _headers(warc_block)
    http_block, separator, body = payload.partition(b"\r\n\r\n")
    if not separator:
        http_block, separator, body = payload.partition(b"\n\n")
    if not separator:
        raise ValueError(f"invalid HTTP response in WARC: {path}")
    _, http_headers = _headers(http_block)
    content_length = http_headers.get("content-length")
    if content_length and content_length.isdigit():
        body = body[: int(content_length)]
    encoding = http_headers.get("content-encoding", "").lower()
    try:
        if encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding == "deflate":
            body = zlib.decompress(body)
    except (gzip.BadGzipFile, zlib.error):
        pass
    return warc_headers, http_headers, body


class TextExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.hidden_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style", "svg", "template"}:
            self.hidden_depth += 1
        if tag == "title":
            self.in_title = True
        if tag == "a" and attributes.get("href"):
            try:
                target = urllib.parse.urljoin(self.base_url, attributes["href"])
            except ValueError:
                # Broken pages occasionally contain arbitrary Unicode or other
                # invalid text in href attributes. It must not discard the page.
                return
            target, _ = urllib.parse.urldefrag(target)
            if urllib.parse.urlparse(target).scheme in {"http", "https"}:
                self.links.add(target)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "template"} and self.hidden_depth:
            self.hidden_depth -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)


def _decode(body: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    charset = charset_match.group(1) if charset_match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def extract_document(
    row: dict[str, Any], corpus_root: Path, max_chars: int
) -> dict[str, Any] | None:
    record_path = Path(row["local_path"])
    if not record_path.is_absolute():
        record_path = corpus_root / record_path
    warc_headers, http_headers, body = read_warc_response(record_path)
    content_type = http_headers.get("content-type", row.get("mime", "")).lower()
    if not any(content_type.startswith(prefix) for prefix in TEXT_TYPES):
        return None
    url = warc_headers.get("warc-target-uri", row["url"])
    decoded = _decode(body, content_type)
    title = ""
    links: list[str] = []
    if "html" in content_type:
        parser = TextExtractor(url)
        parser.feed(decoded)
        title = WHITESPACE.sub(" ", " ".join(parser.title_parts)).strip()
        text = WHITESPACE.sub(" ", " ".join(parser.text_parts)).strip()
        links = sorted(parser.links)
    elif content_type.startswith("application/json"):
        try:
            decoded = json.dumps(json.loads(decoded), ensure_ascii=False)
        except json.JSONDecodeError:
            pass
        text = WHITESPACE.sub(" ", decoded).strip()
    else:
        text = WHITESPACE.sub(" ", decoded).strip()
    if not text:
        return None
    return {
        "url": url,
        "title": title,
        "body": text[:max_chars],
        "domain": urllib.parse.urlparse(url).hostname or "",
        "content_type": content_type.split(";", 1)[0],
        "timestamp": row.get("timestamp", ""),
        "links": links,
    }


def extract_manifest(
    manifest: Path, destination: Path, *, max_chars: int = 250_000
) -> tuple[int, int]:
    corpus_root = manifest.parent
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    count = 0
    skipped = 0
    with (
        manifest.open(encoding="utf-8") as source,
        temporary.open("w", encoding="utf-8") as target,
    ):
        for line in source:
            if not line.strip():
                continue
            document = extract_document(json.loads(line), corpus_root, max_chars)
            if document is None:
                skipped += 1
            else:
                target.write(json.dumps(document, ensure_ascii=False) + "\n")
                count += 1
    temporary.replace(destination)
    return count, skipped
