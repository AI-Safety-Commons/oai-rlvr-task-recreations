#!/usr/bin/env python3
"""Download Stack Exchange site .7z files from the configured Archive.org item."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from time import monotonic
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_ITEM = "stackexchange_20260630_sakura"
LOGGER = logging.getLogger("stackunderflow.download")
CHUNK_SIZE = 1024 * 1024
PROGRESS_INTERVAL_SECONDS = 5.0


def human_size(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def configure_logging(level: str, log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        handlers=handlers,
        force=True,
    )


def file_list(item: str) -> list[dict]:
    LOGGER.info("Fetching metadata for Archive.org item %s", item)
    with urlopen(
        Request(
            f"https://archive.org/metadata/{quote(item)}",
            headers={"User-Agent": "StackUnderflowImporter/1.0"},
        ),
        timeout=60,
    ) as response:
        files = [
            x
            for x in json.load(response).get("files", [])
            if x.get("name", "").endswith(".7z")
        ]
    LOGGER.info("Metadata contains %d site archives", len(files))
    return files


def download(item: str, destination: Path, sites: set[str], dry_run: bool) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    candidates = file_list(item)
    selected = []
    for item_file in candidates:
        remote_name = item_file["name"]
        name = Path(remote_name).name
        slug = name[:-3].lower().replace("_", ".")
        if sites and slug not in sites and name not in sites:
            continue
        selected.append(item_file)
    total_size = sum(int(item_file.get("size", 0)) for item_file in selected)
    LOGGER.info(
        "Selected %d archives (%s) for %s",
        len(selected),
        human_size(total_size),
        destination.resolve(),
    )
    if sites and not selected:
        LOGGER.warning(
            "No archives matched requested sites: %s", ", ".join(sorted(sites))
        )
    downloaded = skipped = 0
    started_all = monotonic()
    for index, item_file in enumerate(selected, 1):
        remote_name = item_file["name"]
        name = Path(remote_name).name
        size = int(item_file.get("size", 0))
        target = destination / name
        print(f"{name}\t{size}\t{target}")
        if dry_run:
            continue
        if target.exists() and target.stat().st_size == size:
            skipped += 1
            LOGGER.info("[%d/%d] Skipping complete %s", index, len(selected), name)
            continue
        temporary = target.with_suffix(target.suffix + ".part")
        if temporary.exists():
            LOGGER.warning(
                "[%d/%d] Restarting incomplete %s (%s already present)",
                index,
                len(selected),
                name,
                human_size(temporary.stat().st_size),
            )
        LOGGER.info(
            "[%d/%d] Downloading %s (%s)",
            index,
            len(selected),
            name,
            human_size(size),
        )
        request = Request(
            f"https://archive.org/download/{quote(item)}/{quote(remote_name)}",
            headers={"User-Agent": "StackUnderflowImporter/1.0"},
        )
        started_file = last_report = monotonic()
        received = 0
        try:
            with (
                urlopen(request, timeout=120) as response,
                temporary.open("wb") as output,
            ):
                while block := response.read(CHUNK_SIZE):
                    output.write(block)
                    received += len(block)
                    current = monotonic()
                    if current - last_report >= PROGRESS_INTERVAL_SECONDS:
                        elapsed = max(current - started_file, 0.001)
                        speed = received / elapsed
                        percent = (received / size * 100) if size else 0
                        remaining = max(size - received, 0) / speed if speed else 0
                        LOGGER.info(
                            "[%d/%d] %s %.1f%% (%s/%s) at %s/s, ETA %.0fs",
                            index,
                            len(selected),
                            name,
                            percent,
                            human_size(received),
                            human_size(size),
                            human_size(speed),
                            remaining,
                        )
                        last_report = current
            if size and received != size:
                raise OSError(f"expected {size} bytes but received {received}")
            temporary.replace(target)
            downloaded += 1
            elapsed = max(monotonic() - started_file, 0.001)
            LOGGER.info(
                "[%d/%d] Finished %s (%s in %.1fs, %s/s)",
                index,
                len(selected),
                name,
                human_size(received),
                elapsed,
                human_size(received / elapsed),
            )
        except Exception:
            LOGGER.exception(
                "[%d/%d] Failed %s; partial download remains at %s",
                index,
                len(selected),
                name,
                temporary,
            )
            raise
    LOGGER.info(
        "Download run complete: %d downloaded, %d skipped, %d selected in %.1fs",
        downloaded,
        skipped,
        len(selected),
        monotonic() - started_all,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item", default=DEFAULT_ITEM)
    parser.add_argument("--destination", type=Path, default=Path("archive"))
    parser.add_argument(
        "--site",
        action="append",
        default=[],
        help="site slug or exact archive filename; repeatable; omit for ALL sites",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show matching files and byte sizes without downloading",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="log destination (default: DESTINATION/download.log)",
    )
    parser.add_argument(
        "--no-log-file", action="store_true", help="write logs only to the terminal"
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    args = parser.parse_args()
    log_file = (
        None
        if args.no_log_file
        else (args.log_file or args.destination / "download.log")
    )
    configure_logging(args.log_level, log_file)
    download(args.item, args.destination, set(args.site), args.dry_run)
