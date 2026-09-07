from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _enabled(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"invalid enabled value: {value!r}")


def load_targets(path: Path) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as source:
        for row_number, row in enumerate(csv.DictReader(source), start=2):
            domain = row["domain"].strip().lower()
            if not DOMAIN.fullmatch(domain):
                raise ValueError(f"invalid domain on {path}:{row_number}: {domain!r}")
            if not _enabled(row["enabled"]):
                continue
            max_records = int(row["max_records"])
            if max_records <= 0:
                raise ValueError(f"max_records must be positive on {path}:{row_number}")
            targets.append(
                {
                    "url": domain,
                    "match_type": "domain",
                    "max_records": max_records,
                    "rank": int(row["rank"]),
                    "category": row["category"].strip(),
                }
            )
    return sorted(targets, key=lambda target: target["rank"])


def merge_targets(
    ranked: list[dict[str, Any]], explicit: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_url = {target["url"]: target for target in ranked}
    for target in explicit:
        previous = by_url.get(target["url"], {})
        by_url[target["url"]] = {**previous, **target}
    return sorted(
        by_url.values(),
        key=lambda target: (target.get("rank", 1_000_000), target["url"]),
    )
