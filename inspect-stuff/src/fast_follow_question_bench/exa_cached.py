"""Cache-only Exa research tools, independent of the HTTP policy gateway."""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

import httpx
from inspect_ai.tool import Tool, tool


def validate_exa_key() -> str:
    key = os.environ.get("EXA_API_KEY", "").strip()
    if not key:
        raise ValueError("exa_cached requires EXA_API_KEY")
    return key


async def _request(endpoint: str, payload: dict, arguments: dict) -> str:
    from .runtime import _advance_clock, _runtime

    key = validate_exa_key()
    runtime = _runtime()
    _advance_clock(runtime, 3)
    runtime.research_calls.append(
        {
            "round": runtime.round_index + 1,
            "phase": runtime.phase,
            "scope": "unknown",
            "kind": f"exa_{endpoint}",
            "arguments": arguments,
            "cost": 3,
        }
    )
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"https://api.exa.ai/{endpoint}",
            headers={"x-api-key": key},
            json=payload,
        )
        response.raise_for_status()
        # Preserve per-page errors and missing content; never fall back to live HTTP.
        return json.dumps(response.json(), ensure_ascii=False)


@tool
def exa_search() -> Tool:
    async def execute(query: str, limit: int = 5) -> str:
        """Search Exa's index and return cached source text without live crawling.

        Args:
            query: Natural-language search query (1–2000 characters).
            limit: Maximum number of results, from 1 to 20.
        """
        if not query.strip() or len(query) > 2000:
            raise ValueError("query must contain 1–2000 characters")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be 1–20")
        return await _request(
            "search",
            {
                "query": query,
                "type": "auto",
                "numResults": limit,
                "contents": {"text": {"maxCharacters": 20000}, "maxAgeHours": -1},
            },
            {"query": query, "limit": limit},
        )

    return execute


@tool
def exa_fetch() -> Tool:
    async def execute(url: str) -> str:
        """Fetch a page's cached text from Exa, without visiting the live page.

        Args:
            url: HTTP or HTTPS URL of the source page to retrieve.
        """
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("url must be an absolute HTTP or HTTPS URL")
        return await _request(
            "contents",
            {"urls": [url], "text": {"maxCharacters": 20000}, "maxAgeHours": -1},
            {"url": url},
        )

    return execute
