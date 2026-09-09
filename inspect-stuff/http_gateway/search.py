"""Exa search augmented by a local embedding index of explicit seed pages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class LocalSearch:
    def __init__(self, pages: dict[str, str], database: Path):
        self.pages = pages
        self.database = database
        self.lock = threading.Lock()
        self.model = None

    def search(self, query: str, limit: int) -> list[dict]:
        if not self.pages:
            return []
        # Lazy loading avoids model downloads entirely for Exa-only runs.
        with self.lock:
            from fastembed import TextEmbedding

            model_name = "BAAI/bge-small-en-v1.5"
            if self.model is None:
                self.model = TextEmbedding(
                    model_name=model_name,
                    cache_dir=str(self.database.parent / "embedding-models"),
                )
            self.database.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.database) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS embeddings "
                    "(id TEXT PRIMARY KEY, vector TEXT NOT NULL)"
                )
                chunks = []
                for url, body in sorted(self.pages.items()):
                    for offset in range(0, len(body), 1200):
                        text = body[offset : offset + 1600]
                        key = hashlib.sha256(
                            (model_name + url + text).encode()
                        ).hexdigest()
                        row = db.execute(
                            "SELECT vector FROM embeddings WHERE id=?", (key,)
                        ).fetchone()
                        if row:
                            vector = json.loads(row[0])
                        else:
                            vector = next(
                                self.model.embed([url + "\n" + text])
                            ).tolist()
                            db.execute(
                                "INSERT OR REPLACE INTO embeddings VALUES (?,?)",
                                (key, json.dumps(vector)),
                            )
                        chunks.append((url, text, vector))
                q = next(self.model.query_embed([query])).tolist()
                ranked = sorted(
                    chunks,
                    key=lambda item: sum(a * b for a, b in zip(q, item[2])),
                    reverse=True,
                )
                results = {}
                for url, text, _ in ranked:
                    if url not in results:
                        results[url] = {
                            "url": url,
                            "title": self.pages[url].splitlines()[0][:200],
                            "text": text,
                            "source": "local",
                        }
                    if len(results) >= limit:
                        break
                return list(results.values())


class SearchService:
    def __init__(
        self, pages: dict[str, str], database: Path, ranking_path: Path | None = None
    ):
        self.local = LocalSearch(pages, database)
        config = (
            json.loads(ranking_path.read_text())
            if ranking_path is not None and ranking_path.exists()
            else {}
        )
        if not isinstance(config, dict) or set(config) - {"pages", "domains"}:
            raise ValueError("search-weights.json accepts only pages and domains maps")
        self.url_weights = {}
        self.domain_weights = {}
        for group, target in (
            ("pages", self.url_weights),
            ("domains", self.domain_weights),
        ):
            entries = config.get(group, {})
            if not isinstance(entries, dict):
                raise ValueError(f"{group} must be a weight map")  # noqa: TRY004
            for key, value in entries.items():
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError(f"Invalid weight for {key}")
                if group == "domains":
                    key = key.lower().rstrip(".")
                    if not key or any(c in key for c in "/:*@ ?#"):
                        raise ValueError("Domain keys must be bare hostnames")
                elif (
                    urlsplit(key).scheme not in {"http", "https"}
                    or not urlsplit(key).hostname
                ):
                    raise ValueError("Page keys must be absolute HTTP(S) URLs")
                target[key] = value
        self.weights = {
            "exa": float(os.environ.get("SEARCH_EXA_WEIGHT", "1")),
            "local": float(os.environ.get("SEARCH_LOCAL_WEIGHT", "1")),
        }
        if any(not math.isfinite(w) or w < 0 for w in self.weights.values()):
            raise ValueError("Search weights must be finite, nonnegative numbers")
        if not any(self.weights.values()):
            raise ValueError("At least one search weight must be positive")

    def page_weight(self, url: str) -> float:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        # Most-specific domain rule wins; example.org also matches subdomains.
        matches = [
            domain
            for domain in self.domain_weights
            if host == domain or host.endswith("." + domain)
        ]
        domain_weight = self.domain_weights[max(matches, key=len)] if matches else 1
        return domain_weight * self.url_weights.get(url, 1)

    async def search(self, query: str, limit: int, source: str) -> dict:
        if not query.strip() or len(query) > 2000:
            raise ValueError("query must contain 1–2000 characters")
        if not 1 <= limit <= 20 or source not in {"all", "web", "local"}:
            raise ValueError("limit must be 1–20; source must be all, web, or local")

        async def web():
            key = os.environ.get("EXA_API_KEY")
            if not key:
                raise RuntimeError("EXA_API_KEY is not configured")
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.post(
                    "https://api.exa.ai/search",
                    headers={"x-api-key": key},
                    json={
                        "query": query,
                        "type": "auto",
                        "numResults": max(limit, 20)
                        if self.url_weights or self.domain_weights
                        else limit,
                        "contents": {"text": {"maxCharacters": 1600}},
                    },
                )
                response.raise_for_status()
                return [
                    {
                        "url": r["url"],
                        "title": r.get("title", ""),
                        "text": r.get("text", ""),
                        "source": "exa",
                    }
                    for r in response.json()["results"]
                ]

        # Explicit single-provider requests remain usable regardless of blend weights.
        weights = self.weights if source == "all" else {"exa": 1, "local": 1}
        names, jobs = [], []
        if source != "local" and weights["exa"] > 0:
            names.append("exa")
            jobs.append(web())
        if source != "web" and weights["local"] > 0:
            names.append("local")
            # Apply boosts before truncating local candidates to the output limit.
            candidates = max(limit, len(self.local.pages))
            jobs.append(asyncio.to_thread(self.local.search, query, candidates))
        outputs = await asyncio.gather(*jobs, return_exceptions=True)
        errors, lists = {}, {}
        for name, output in zip(names, outputs):
            if isinstance(output, Exception):
                # Never return HTTP exception details containing credentials.
                errors[name] = (
                    "EXA_API_KEY is not configured"
                    if name == "exa" and not os.environ.get("EXA_API_KEY")
                    else f"{name} search failed ({type(output).__name__})"
                )
            else:
                lists[name] = output
        # Reciprocal rank fusion; a local page overrides the same live URL.
        scores, rows = {}, {}
        for name in ("exa", "local"):
            for rank, row in enumerate(lists.get(name, []), 1):
                url = row["url"]
                multiplier = self.page_weight(url)
                if multiplier == 0:
                    continue
                scores[url] = scores.get(url, 0) + weights[name] * multiplier / (
                    60 + rank
                )
                rows[url] = row
        ordered = sorted(
            rows, key=lambda u: (-scores[u], rows[u]["source"] != "local", u)
        )
        return {"results": [rows[u] for u in ordered[:limit]], "errors": errors}
