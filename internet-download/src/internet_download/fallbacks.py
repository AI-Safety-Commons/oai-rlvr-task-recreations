from __future__ import annotations

import html
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UnavailableResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def load_unavailable_sites(path: Path) -> dict[str, dict[str, Any]]:
    with path.open("rb") as source:
        raw = tomllib.load(source).get("sites", {})
    return {domain.lower().rstrip("."): dict(policy) for domain, policy in raw.items()}


def load_unavailable_routes(path: Path) -> dict[str, dict[str, Any]]:
    with path.open("rb") as source:
        raw = tomllib.load(source).get("routes", {})
    return {domain.lower().rstrip("."): dict(policy) for domain, policy in raw.items()}


def policy_for_host(
    host: str, policies: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    normalized = host.partition(":")[0].lower().rstrip(".")
    candidates = sorted(policies, key=len, reverse=True)
    for domain in candidates:
        if normalized == domain or normalized.endswith(f".{domain}"):
            return policies[domain]
    return None


def policy_for_request(
    host: str,
    path: str,
    query: dict[str, list[str]],
    policies: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    policy = policy_for_host(host, policies)
    if policy is None:
        return None
    prefixes = policy.get("path_prefixes", ["/"])
    if not any(path.startswith(prefix) for prefix in prefixes):
        return None
    required_parameters = policy.get("query_parameters", [])
    if not all(parameter in query for parameter in required_parameters):
        return None
    return policy


def render_unavailable(policy: dict[str, Any]) -> UnavailableResponse:
    status = int(policy["status"])
    title = html.escape(str(policy["title"]))
    message = html.escape(str(policy["message"]))
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{status} {title}</title></head><body><h1>{title}</h1>"
        f"<p>{message}</p></body></html>"
    ).encode()
    headers = {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "Content-Length": str(len(body)),
    }
    if retry_after := policy.get("retry_after"):
        headers["Retry-After"] = str(int(retry_after))
    return UnavailableResponse(status=status, headers=headers, body=body)
