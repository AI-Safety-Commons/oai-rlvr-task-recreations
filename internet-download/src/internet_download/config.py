from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as config_file:
        config = tomllib.load(config_file)
    if not isinstance(config.get("commoncrawl", {}), dict):
        raise TypeError("[commoncrawl] must be a table")
    if not isinstance(config.get("kiwix", {}), dict):
        raise TypeError("[kiwix] must be a table")
    return config


def resolve_output(config_path: Path, value: str) -> Path:
    output = Path(value)
    return output if output.is_absolute() else config_path.parent / output
