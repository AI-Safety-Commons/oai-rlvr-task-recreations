from __future__ import annotations

import html
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .search import parse_search_query, query_index


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def query_kiwix(base_url: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
    parameters = urllib.parse.urlencode(
        {
            "pattern": query,
            "books.filter.lang": "eng",
            "pageLength": limit,
            "format": "xml",
        }
    )
    url = f"{base_url.rstrip('/')}/search?{parameters}"
    request = urllib.request.Request(url, headers={"Accept": "application/xml"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            root = ET.fromstring(response.read())
    except (OSError, urllib.error.URLError, ET.ParseError):
        return []
    results: list[dict[str, Any]] = []
    entries = [
        node for node in root.iter() if _local_name(node.tag) in {"entry", "item"}
    ]
    for entry in entries:
        values: dict[str, str] = {}
        link = ""
        for child in entry.iter():
            name = _local_name(child.tag)
            if name == "link" and not link:
                link = child.attrib.get("href", "") or (child.text or "")
            elif name in {"title", "summary", "description", "content"}:
                values.setdefault(name, "".join(child.itertext()).strip())
        if not link:
            continue
        snippet = (
            values.get("summary")
            or values.get("description")
            or values.get("content", "")
        )
        results.append(
            {
                "url": urllib.parse.urljoin(base_url.rstrip("/") + "/", link),
                "title": values.get("title", link),
                "domain": urllib.parse.urlparse(base_url).hostname or "kiwix",
                "snippet": snippet,
                "source": "kiwix",
            }
        )
        if len(results) >= limit:
            break
    return results


def query_federated(
    database: Path,
    query: str,
    limit: int = 10,
    kiwix_url: str | None = None,
    source_boosts: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    per_source = max(limit, 10)
    groups: list[list[dict[str, Any]]] = [query_index(database, query, per_source, source_boosts)]
    text, sites = parse_search_query(query)
    if kiwix_url and text and not sites:
        groups.append(query_kiwix(kiwix_url, text, per_source))
    merged: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    for group in groups:
        for rank, result in enumerate(group, 1):
            url = str(result["url"])
            merged.setdefault(url, result)
            source = str(result.get("source", ""))
            boost = float((source_boosts or {}).get(source, 1.0))
            scores[url] = scores.get(url, 0.0) + boost / (60 + rank)
    ordered = sorted(
        merged.values(), key=lambda item: scores[str(item["url"])], reverse=True
    )
    # An overlay edit is the canonical view of the same Wikipedia article. Do
    # not present the unchanged ZIM hit beside it when both are returned.
    overlay_keys = {
        _article_key(str(item["url"]))
        for item in ordered
        if item.get("source") == "kiwix-overlay"
    }
    if overlay_keys:
        ordered = [
            item
            for item in ordered
            if not (
                item.get("source") == "kiwix"
                and _article_key(str(item["url"])) in overlay_keys
            )
        ]
    for result in ordered:
        result["federated_score"] = scores[str(result["url"])]
    return ordered[:limit]


def _article_key(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path).strip("/").lower()
    for prefix in ("wiki/", "content/"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    return path.replace("_", " ")


def serve_search(
    database: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8091,
    kiwix_url: str | None = None,
) -> None:
    database = database.resolve()
    kiwix_url = kiwix_url or os.environ.get("KIWIX_URL")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                body = json.dumps(
                    {
                        "status": "ok",
                        "database": str(database),
                        "kiwix": kiwix_url,
                    }
                ).encode()
                self._send(HTTPStatus.OK, "application/json", body)
                return
            if parsed.path not in {"/", "/search", "/api/search"}:
                self._send(HTTPStatus.NOT_FOUND, "text/plain", b"Not found\n")
                return
            params = urllib.parse.parse_qs(parsed.query)
            query = params.get("q", [""])[0].strip()
            try:
                limit = max(1, min(100, int(params.get("limit", ["10"])[0])))
            except ValueError:
                limit = 10
            results = (
                query_federated(database, query, limit, kiwix_url) if query else []
            )
            if parsed.path == "/api/search":
                body = json.dumps(
                    {"query": query, "results": results}, ensure_ascii=False
                ).encode()
                self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
                return
            items = "".join(
                f'<li><a href="{html.escape(str(item["url"]), quote=True)}">'
                f"{html.escape(str(item.get('title') or item['url']))}</a> "
                f"<small>{html.escape(str(item.get('source', '')))}</small>"
                f"<p>{html.escape(str(item.get('snippet', '')))}</p></li>"
                for item in results
            )
            page = (
                "<!doctype html><meta charset=utf-8><title>Offline search</title>"
                "<style>body{font:16px system-ui;max-width:850px;margin:3rem auto;padding:0 1rem}"
                "input{width:75%;padding:.6rem}li{margin:1.2rem 0}p{margin:.25rem 0}</style>"
                f'<h1>Offline search</h1><form action="/search"><input name="q" value="{html.escape(query, quote=True)}" autofocus>'
                "<button>Search</button></form>"
                f"<ol>{items}</ol>"
            ).encode()
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", page)

        def log_message(self, format: str, *args: object) -> None:
            print(f"search: {format % args}")

    print(
        f"Search listening on http://{host}:{port} (SQLite={database}, Kiwix={kiwix_url or 'off'})"
    )
    ThreadingHTTPServer((host, port), Handler).serve_forever()
