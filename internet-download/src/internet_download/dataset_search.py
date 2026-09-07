from __future__ import annotations

import csv
import io
import json
import mimetypes
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from .extract import WHITESPACE, TextExtractor


def _document(
    entry: dict[str, Any], body: str, part: str, content_type: str
) -> dict[str, str]:
    url = str(entry["url"])
    if part:
        url = f"{url}#{urllib.parse.quote(part, safe='=-_')}"
    label = str(entry.get("label") or Path(str(entry["path"])).name)
    title = f"{label} — {part}" if part else label
    return {
        "url": url,
        "title": title,
        "body": WHITESPACE.sub(" ", body).strip(),
        "domain": urllib.parse.urlparse(str(entry["url"])).hostname or "",
        "content_type": content_type,
        "timestamp": "",
        "source": f"dataset:{entry.get('provider', 'unknown')}",
    }


def _chunks(values: Iterable[str], max_chars: int) -> Iterator[str]:
    parts: list[str] = []
    size = 0
    for value in values:
        value = value.strip()
        if not value:
            continue
        if parts and size + len(value) + 1 > max_chars:
            yield "\n".join(parts)
            parts, size = [], 0
        while len(value) > max_chars:
            if parts:
                yield "\n".join(parts)
                parts, size = [], 0
            yield value[:max_chars]
            value = value[max_chars:]
        if value:
            parts.append(value)
            size += len(value) + 1
    if parts:
        yield "\n".join(parts)


def _csv_chunks(
    stream: TextIO, max_chars: int, rows_per_document: int
) -> Iterator[tuple[str, str]]:
    reader = csv.reader(stream)
    try:
        header = next(reader)
    except StopIteration:
        return
    heading = " | ".join(header)
    batch: list[str] = []
    size = len(heading)
    start = 2
    row_number = 1
    for row_number, row in enumerate(reader, 2):
        rendered = " | ".join(row)
        if batch and (
            len(batch) >= rows_per_document or size + len(rendered) + 1 > max_chars
        ):
            yield f"rows={start}-{row_number - 1}", "\n".join([heading, *batch])
            batch, size, start = [], len(heading), row_number
        batch.append(rendered)
        size += len(rendered) + 1
    if batch:
        yield f"rows={start}-{row_number}", "\n".join([heading, *batch])
    elif header:
        yield "rows=1-1", heading


def _json_record_chunks(
    stream: TextIO, max_chars: int, rows_per_document: int
) -> Iterator[tuple[str, str]]:
    batch: list[str] = []
    size = 0
    start = 1
    row_number = 0
    for row_number, line in enumerate(stream, 1):
        line = line.strip()
        if not line:
            continue
        if batch and (
            len(batch) >= rows_per_document or size + len(line) + 1 > max_chars
        ):
            yield f"records={start}-{row_number - 1}", "\n".join(batch)
            batch, size, start = [], 0, row_number
        batch.append(line[:max_chars])
        size += min(len(line), max_chars) + 1
    if batch:
        yield f"records={start}-{row_number}", "\n".join(batch)


def _xml_text(stream: BinaryIO, max_chars: int) -> Iterator[tuple[str, str]]:
    values = (
        text
        for _event, element in ET.iterparse(stream, events=("end",))
        for text in (element.text, element.tail)
        if text
    )
    for index, body in enumerate(_chunks(values, max_chars), 1):
        yield f"part={index}", body


def _xlsx_rows(
    path: Path, max_chars: int, rows_per_document: int
) -> Iterator[tuple[str, str]]:
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.parse(archive.open("xl/sharedStrings.xml")).getroot()
            for item in root:
                shared.append(
                    "".join(
                        node.text or ""
                        for node in item.iter()
                        if node.tag.endswith("}t")
                    )
                )
        sheets = sorted(
            name
            for name in archive.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        )
        for sheet_index, name in enumerate(sheets, 1):
            rows: list[str] = []
            for _event, element in ET.iterparse(archive.open(name), events=("end",)):
                if not element.tag.endswith("}row"):
                    continue
                cells: list[str] = []
                for cell in element:
                    if not cell.tag.endswith("}c"):
                        continue
                    value = next(
                        (node.text or "" for node in cell if node.tag.endswith("}v")),
                        "",
                    )
                    if cell.attrib.get("t") == "s" and value.isdigit():
                        position = int(value)
                        value = shared[position] if position < len(shared) else value
                    elif cell.attrib.get("t") == "inlineStr":
                        value = "".join(
                            node.text or ""
                            for node in cell.iter()
                            if node.tag.endswith("}t")
                        )
                    cells.append(value)
                rows.append(" | ".join(cells))
                element.clear()
            for part, body in enumerate(_chunks(rows, max_chars), 1):
                yield f"sheet={sheet_index}&part={part}", body


def _zip_documents(
    path: Path, max_chars: int, rows_per_document: int
) -> Iterator[tuple[str, str, str]]:
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if member.is_dir() or member.file_size == 0:
                continue
            suffix = Path(member.filename).suffix.lower()
            if suffix not in {".csv", ".json", ".jsonl", ".xml", ".txt"}:
                continue
            with archive.open(member) as raw:
                if suffix == ".csv":
                    text = io.TextIOWrapper(
                        raw, encoding="utf-8-sig", errors="replace", newline=""
                    )
                    for part, body in _csv_chunks(text, max_chars, rows_per_document):
                        yield f"file={member.filename}&{part}", body, "text/csv"
                elif suffix in {".jsonl"}:
                    text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                    for part, body in _json_record_chunks(
                        text, max_chars, rows_per_document
                    ):
                        yield f"file={member.filename}&{part}", body, "application/json"
                elif suffix == ".xml":
                    for part, body in _xml_text(raw, max_chars):
                        yield f"file={member.filename}&{part}", body, "application/xml"
                else:
                    text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                    for part, body in enumerate(_chunks(text, max_chars), 1):
                        yield f"file={member.filename}&part={part}", body, "text/plain"


def extract_dataset_file(
    entry: dict[str, Any], path: Path, max_chars: int, rows_per_document: int
) -> Iterator[dict[str, str]]:
    suffix = path.suffix.lower()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts: Iterable[tuple[str, str, str]]
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
            for part, body in _csv_chunks(stream, max_chars, rows_per_document):
                yield _document(entry, body, part, "text/csv")
        return
    if suffix in {".jsonl", ".jsonrecords"}:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for part, body in _json_record_chunks(stream, max_chars, rows_per_document):
                yield _document(entry, body, part, "application/json")
        return
    if suffix == ".zip":
        parts = _zip_documents(path, max_chars, rows_per_document)
    elif suffix == ".xlsx":
        parts = (
            (part, body, content_type)
            for part, body in _xlsx_rows(path, max_chars, rows_per_document)
        )
    elif suffix == ".xml":
        with path.open("rb") as stream:
            for part, body in _xml_text(stream, max_chars):
                yield _document(entry, body, part, "application/xml")
        return
    elif suffix in {".html", ".htm"}:
        parser = TextExtractor(str(entry["url"]))
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        parts = (("", " ".join(parser.text_parts), "text/html"),)
    elif suffix == ".pdf":
        try:
            output = subprocess.run(
                ["pdftotext", str(path), "-"], check=True, capture_output=True
            ).stdout.decode("utf-8", errors="replace")
        except (FileNotFoundError, subprocess.CalledProcessError):
            return
        parts = (
            (f"part={index}", body, "application/pdf")
            for index, body in enumerate(_chunks([output], max_chars), 1)
        )
    elif suffix in {".json", ".txt", ".md"}:
        with path.open(encoding="utf-8", errors="replace") as stream:
            parts = (
                (f"part={index}", body, content_type)
                for index, body in enumerate(_chunks(stream, max_chars), 1)
            )
            for part, body, kind in parts:
                yield _document(entry, body, part, kind)
        return
    else:
        return
    for part, body, kind in parts:
        if body.strip():
            yield _document(entry, body, part, kind)


def extract_dataset_manifest(
    manifest: Path,
    destination: Path,
    *,
    max_chars: int = 250_000,
    rows_per_document: int = 100,
) -> tuple[int, int]:
    latest: dict[str, dict[str, Any]] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            latest[str(entry.get("url", entry.get("path", "")))] = entry
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    count = skipped = 0
    files_root = manifest.parent / "files"
    with temporary.open("w", encoding="utf-8") as target:
        for entry in latest.values():
            path = files_root / str(entry.get("path", ""))
            if entry.get("status") == "error" or not path.is_file():
                skipped += 1
                continue
            before = count
            try:
                for document in extract_dataset_file(
                    entry, path, max_chars, rows_per_document
                ):
                    target.write(json.dumps(document, ensure_ascii=False) + "\n")
                    count += 1
            except (OSError, csv.Error, ET.ParseError, zipfile.BadZipFile, ValueError):
                skipped += 1
                continue
            if count == before:
                skipped += 1
    temporary.replace(destination)
    return count, skipped


def extract_overlay_database(
    database: Path, destination: Path, *, max_chars: int = 250_000
) -> tuple[int, int]:
    """Export the current revision of each Kiwix overlay page as search docs."""
    import sqlite3

    if not database.exists():
        return 0, 0
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT r.* FROM revisions r JOIN ("
            "SELECT book,title,max(id) id FROM revisions GROUP BY book,title"
            ") latest ON latest.id=r.id ORDER BY r.id"
        ).fetchall()
    finally:
        connection.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    count = 0
    with temporary.open("w", encoding="utf-8") as target:
        for row in rows:
            parser = TextExtractor(
                f"http://kiwix-overlay/wiki/{row['book']}/{row['title']}"
            )
            parser.feed(str(row["html"]))
            body = WHITESPACE.sub(" ", " ".join(parser.text_parts)).strip()
            body = (f"{row['summary']}\n{body}").strip()[:max_chars]
            if not body:
                continue
            encoded = urllib.parse.quote(str(row["title"]).replace(" ", "_"), safe="")
            entry = {
                "url": f"http://kiwix-overlay/wiki/{row['book']}/{encoded}",
                "title": f"{row['title']} — Kiwix edit",
                "body": body,
                "domain": "kiwix-overlay",
                "content_type": "text/html",
                "timestamp": str(row["created_at"]),
                "source": "kiwix-overlay",
            }
            target.write(json.dumps(entry, ensure_ascii=False) + "\n")
            count += 1
    temporary.replace(destination)
    return count, len(rows) - count


def initialize_overlay_cursor(database: Path, cursor: Path) -> int:
    """Record the current revision watermark; later syncs only see new edits."""
    import sqlite3

    if not database.exists():
        value = 0
    else:
        with sqlite3.connect(database) as connection:
            value = int(connection.execute("SELECT coalesce(max(id), 0) FROM revisions").fetchone()[0])
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(f"{value}\n", encoding="utf-8")
    return value


def sync_overlay(
    overlay_database: Path,
    search_database: Path,
    cursor: Path,
    tags: list[str] | None = None,
) -> tuple[int, int]:
    """Index only non-snapshot revisions newer than the persisted watermark."""
    import sqlite3

    if not overlay_database.exists() or not search_database.exists():
        return 0, 0
    try:
        since = int(cursor.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        since = initialize_overlay_cursor(overlay_database, cursor)
    with sqlite3.connect(overlay_database) as source:
        source.row_factory = sqlite3.Row
        rows = source.execute(
            "SELECT * FROM revisions WHERE id > ? AND is_snapshot=0 ORDER BY id", (since,)
        ).fetchall()
    added = 0
    connection = sqlite3.connect(search_database, timeout=30)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        with connection:
            for row in rows:
                parser = TextExtractor(f"http://kiwix-overlay/wiki/{row['book']}/{row['title']}")
                parser.feed(str(row["html"]))
                body = WHITESPACE.sub(" ", " ".join(parser.text_parts)).strip()
                body = " ".join(
                    [
                        *(tags or []),
                        f"revision {row['id']} author {row['username']} {row['summary']}",
                        body,
                    ]
                )
                body = (f"{row['summary']}\n{body}").strip()
                encoded = urllib.parse.quote(str(row["title"]).replace(" ", "_"), safe="")
                values = (
                    f"http://kiwix-overlay/wiki/{row['book']}/{encoded}",
                    f"{row['title']} — Kiwix edit",
                    body[:250_000], "kiwix-overlay", "text/html", str(row["created_at"]),
                    "kiwix-overlay",
                )
                old = connection.execute(
                    "SELECT page_rowid FROM live_documents WHERE source=? AND external_id=?",
                    ("kiwix-overlay", str(row["id"])),
                ).fetchone()
                if old:
                    connection.execute(
                        "UPDATE pages SET url=?,title=?,body=?,domain=?,content_type=?,timestamp=?,source=? WHERE rowid=?",
                        (*values, old[0]),
                    )
                else:
                    inserted = connection.execute(
                        "INSERT INTO pages(url,title,body,domain,content_type,timestamp,source) VALUES(?,?,?,?,?,?,?)",
                        values,
                    )
                    connection.execute(
                        "INSERT INTO live_documents(source,external_id,page_rowid) VALUES(?,?,?)",
                        ("kiwix-overlay", str(row["id"]), inserted.lastrowid),
                    )
                    added += 1
    finally:
        connection.close()
    newest = max((int(row["id"]) for row in rows), default=since)
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(f"{newest}\n", encoding="utf-8")
    return added, len(rows) - added


def initialize_webhooks_cursor(events: Path, cursor: Path) -> int:
    value = 0
    if events.exists():
        for line in events.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = max(value, int(json.loads(line)["id"]))
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(f"{value}\n", encoding="utf-8")
    return value


def sync_webhooks(
    events: Path, search_database: Path, cursor: Path, tags: list[str] | None = None
) -> tuple[int, int]:
    import sqlite3

    if not events.exists() or not search_database.exists():
        return 0, 0
    try:
        since = int(cursor.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        since = initialize_webhooks_cursor(events, cursor)
    records = [
        json.loads(line)
        for line in events.read_text(encoding="utf-8").splitlines()
        if line.strip() and int(json.loads(line)["id"]) > since
    ]
    connection = sqlite3.connect(search_database, timeout=30)
    added = 0
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        with connection:
            for event in records:
                event_id = str(event["id"])
                payload = json.dumps(event.get("json", {}), ensure_ascii=False)
                body = " ".join([*(tags or []), payload])
                url = f"http://webhooks.com{event.get('path', '/') }#event-{event_id}"
                values = (url, f"webhooks.com event {event_id}", body, "webhooks.com", "application/json", str(event.get("received_at", "")), "webhooks.com")
                old = connection.execute("SELECT page_rowid FROM live_documents WHERE source=? AND external_id=?", ("webhooks.com", event_id)).fetchone()
                if old:
                    connection.execute("UPDATE pages SET url=?,title=?,body=?,domain=?,content_type=?,timestamp=?,source=? WHERE rowid=?", (*values, old[0]))
                else:
                    inserted = connection.execute("INSERT INTO pages(url,title,body,domain,content_type,timestamp,source) VALUES(?,?,?,?,?,?,?)", values)
                    connection.execute("INSERT INTO live_documents(source,external_id,page_rowid) VALUES(?,?,?)", ("webhooks.com", event_id, inserted.lastrowid))
                    added += 1
    finally:
        connection.close()
    newest = max((int(event["id"]) for event in records), default=since)
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(f"{newest}\n", encoding="utf-8")
    return added, len(records) - added
