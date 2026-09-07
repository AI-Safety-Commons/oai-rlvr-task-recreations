from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .http import atomic_write, request


@dataclass(frozen=True)
class DatasetFile:
    provider: str
    kind: str
    url: str
    path: str
    label: str = ""


def _read(url: str, accept: str | None = None) -> bytes:
    headers = {"Accept": accept} if accept else None
    with request(url, headers=headers, timeout=300, attempts=6) as response:
        return response.read()


def _safe(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._@+-]+", "_", value).strip("._")
    return value[:180] or hashlib.sha256(value.encode()).hexdigest()


def _write_catalog(output: Path, name: str, body: bytes) -> Path:
    destination = output / "catalogs" / name
    atomic_write(destination, body)
    return destination


def _configured_files(config: dict[str, Any]) -> list[DatasetFile]:
    result = []
    for item in config.get("files", []):
        url = str(item["url"])
        path = item.get("path")
        if not path:
            filename = Path(urllib.parse.urlsplit(url).path).name or "response"
            path = f"{item['provider']}/{item.get('kind', 'data')}/{_safe(filename)}"
        result.append(
            DatasetFile(
                str(item["provider"]),
                str(item.get("kind", "data")),
                url,
                str(path),
                str(item.get("label", "")),
            )
        )
    return result


def _ilostat_files(config: dict[str, Any], output: Path) -> list[DatasetFile]:
    if not config.get("enabled", True):
        return []
    base = str(config.get("base_url", "https://rplumber.ilo.org/files")).rstrip("/")
    toc_url = f"{base}/indicator/table_of_contents_en.csv"
    body = _read(toc_url, "text/csv")
    _write_catalog(output, "ilostat-indicators.csv", body)
    rows = csv.DictReader(body.decode("utf-8-sig").splitlines())
    result = []
    for row in rows:
        identifier = (row.get("id") or "").strip()
        if not identifier:
            continue
        filename = (
            identifier if identifier.endswith(".csv.gz") else f"{identifier}.csv.gz"
        )
        result.append(
            DatasetFile(
                "ilostat",
                "indicator",
                f"{base}/indicator/{urllib.parse.quote(filename, safe='._-')}",
                f"ilostat/indicator/{_safe(filename)}",
                row.get("indicator.label", ""),
            )
        )
    for name in config.get("dictionaries", []):
        filename = str(name)
        result.append(
            DatasetFile(
                "ilostat",
                "dictionary",
                f"{base}/dic/{urllib.parse.quote(filename, safe='._-')}",
                f"ilostat/dic/{_safe(filename)}",
                filename,
            )
        )
    return result


def _oecd_files(config: dict[str, Any], output: Path) -> list[DatasetFile]:
    if not config.get("enabled", True):
        return []
    base = str(config.get("base_url", "https://sdmx.oecd.org/public/rest")).rstrip("/")
    catalog_url = f"{base}/dataflow/all/all/latest?references=none"
    body = _read(catalog_url, "application/vnd.sdmx.structure+xml;version=2.0")
    _write_catalog(output, "oecd-dataflows.xml", body)
    root = ET.fromstring(body)
    flows: set[tuple[str, str, str]] = set()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "Dataflow":
            continue
        agency = element.attrib.get("agencyID")
        identifier = element.attrib.get("id")
        version = element.attrib.get("version", "latest")
        if agency and identifier:
            flows.add((agency, identifier, version))
    include = re.compile(str(config.get("include", ".*")), re.IGNORECASE)
    exclude = re.compile(str(config.get("exclude", "^$")), re.IGNORECASE)
    result = []
    for agency, identifier, version in sorted(flows):
        identity = f"{agency},{identifier},{version}"
        if not include.search(identity) or exclude.search(identity):
            continue
        query = urllib.parse.urlencode(
            {"dimensionAtObservation": "AllDimensions", "format": "csvfile"}
        )
        result.append(
            DatasetFile(
                "oecd",
                "dataflow",
                f"{base}/data/{identity}/all?{query}",
                f"oecd/data/{_safe(agency)}/{_safe(identifier)}--{_safe(version)}.csv",
                identity,
            )
        )
        if config.get("include_structures", True):
            result.append(
                DatasetFile(
                    "oecd",
                    "structure",
                    f"{base}/dataflow/{agency}/{identifier}/{version}?references=all",
                    f"oecd/structure/{_safe(agency)}/{_safe(identifier)}--{_safe(version)}.xml",
                    identity,
                )
            )
    return result


def _datausa_files(config: dict[str, Any], output: Path) -> list[DatasetFile]:
    if not config.get("enabled", True):
        return []
    base = str(config.get("base_url", "https://api.datausa.io/tesseract")).rstrip("/")
    catalog_url = f"{base}/cubes"
    body = _read(catalog_url, "application/json")
    _write_catalog(output, "datausa-cubes.json", body)
    catalog = json.loads(body)
    include = re.compile(str(config.get("include", ".*")), re.IGNORECASE)
    exclude = re.compile(str(config.get("exclude", "^$")), re.IGNORECASE)
    result = []
    for cube in catalog.get("cubes", []):
        name = str(cube["name"])
        if not include.search(name) or exclude.search(name):
            continue
        result.append(
            DatasetFile(
                "datausa",
                "cube-schema",
                f"{base}/cubes/{urllib.parse.quote(name, safe='_-')}",
                f"datausa/cubes/{_safe(name)}.json",
                name,
            )
        )
    # Data USA cubes can contain billions of cells. Full-cube exports therefore
    # remain explicit queries; the catalog and every selected schema are automatic.
    for query in config.get("queries", []):
        name = str(query["name"])
        params = str(query["params"])
        extension = str(query.get("format", "parquet"))
        result.append(
            DatasetFile(
                "datausa",
                "cube-data",
                f"{base}/data.{extension}?{params}",
                f"datausa/data/{_safe(name)}.{_safe(extension)}",
                name,
            )
        )
    return result


def write_dataset_plan(
    config: dict[str, Any], output: Path
) -> tuple[int, dict[str, int]]:
    output.mkdir(parents=True, exist_ok=True)
    entries = _configured_files(config)
    errors = []
    discoverers = (
        ("ilostat", _ilostat_files),
        ("oecd", _oecd_files),
        ("datausa", _datausa_files),
    )
    for provider, discover in discoverers:
        try:
            entries.extend(discover(config.get(provider, {}), output))
        except Exception as error:  # noqa: BLE001 - providers are independent.
            errors.append({"provider": provider, "error": str(error)})
    error_path = output / "catalog-errors.json"
    if errors:
        atomic_write(error_path, (json.dumps(errors, indent=2) + "\n").encode())
    elif error_path.exists():
        error_path.unlink()
    unique = {entry.url: entry for entry in entries}
    ordered = sorted(
        unique.values(), key=lambda item: (item.provider, item.kind, item.url)
    )
    plan = output / "plan.jsonl"
    body = "".join(json.dumps(asdict(item), sort_keys=True) + "\n" for item in ordered)
    atomic_write(plan, body.encode())
    counts: dict[str, int] = {}
    for item in ordered:
        counts[item.provider] = counts.get(item.provider, 0) + 1
    return len(ordered), counts


def read_dataset_plan(path: Path) -> list[DatasetFile]:
    return [
        DatasetFile(**json.loads(line))
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def _append_manifest(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _download(
    entry: DatasetFile, output: Path, max_file_bytes: int | None
) -> tuple[int, str]:
    destination = output / "files" / entry.path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return destination.stat().st_size, "existing"
    partial = destination.with_suffix(destination.suffix + ".part")
    existing = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={existing}-"} if existing else {}
    try:
        response = request(entry.url, headers=headers, timeout=600, attempts=6)
    except urllib.error.HTTPError as error:
        if existing and error.code == 416:
            partial.replace(destination)
            return destination.stat().st_size, "downloaded"
        raise
    with response:
        append = existing > 0 and response.status == 206
        if existing and not append:
            existing = 0
        length = response.headers.get("Content-Length")
        expected = existing + int(length) if length and length.isdigit() else None
        if (
            max_file_bytes is not None
            and expected is not None
            and expected > max_file_bytes
        ):
            return 0, "skipped-file-cap"
        mode = "ab" if append else "wb"
        size = existing
        digest = hashlib.sha256()
        if append:
            with partial.open("rb") as prior:
                for chunk in iter(lambda: prior.read(1024 * 1024), b""):
                    digest.update(chunk)
        with partial.open(mode) as stream:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if max_file_bytes is not None and size > max_file_bytes:
                    stream.flush()
                    os.fsync(stream.fileno())
                    return size, "partial-file-cap"
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    partial.replace(destination)
    return size, digest.hexdigest()


def fetch_dataset_plan(
    plan: Path,
    output: Path,
    *,
    max_total_bytes: int | None,
    max_file_bytes: int | None,
    providers: Iterable[str] = (),
) -> dict[str, int]:
    selected = set(providers)
    totals = {"downloaded": 0, "existing": 0, "skipped": 0, "bytes": 0}
    manifest = output / "manifest.jsonl"

    def disk_usage() -> int:
        return sum(path.stat().st_size for path in output.rglob("*") if path.is_file())

    for entry in read_dataset_plan(plan):
        if selected and entry.provider not in selected:
            continue
        before = disk_usage()
        remaining = None if max_total_bytes is None else max_total_bytes - before
        # Reserve room for the manifest line written after the response.
        if remaining is not None and remaining <= 2048:
            break
        partial = output / "files" / entry.path
        partial = partial.with_suffix(partial.suffix + ".part")
        resumed_bytes = partial.stat().st_size if partial.exists() else 0
        growth_cap = None if remaining is None else remaining - 2048
        if growth_cap is None:
            file_cap = max_file_bytes
        else:
            file_cap = min(
                max_file_bytes or resumed_bytes + growth_cap,
                resumed_bytes + growth_cap,
            )
        try:
            size, status = _download(entry, output, file_cap)
            if status == "existing":
                totals["existing"] += 1
            elif status.startswith(("skipped", "partial")):
                totals["skipped"] += 1
            else:
                totals["downloaded"] += 1
            _append_manifest(
                manifest, {**asdict(entry), "size": size, "status": status}
            )
            totals["bytes"] += max(0, disk_usage() - before)
        # One retired dataflow must not discard progress from independent hosts.
        except Exception as error:  # noqa: BLE001
            totals["skipped"] += 1
            _append_manifest(
                manifest,
                {**asdict(entry), "size": 0, "status": "error", "error": str(error)},
            )
    return totals
