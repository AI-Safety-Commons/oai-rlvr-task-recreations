#!/usr/bin/env python3
"""Monitor the on-disk progress of a Kiwix ZIM download."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def human_bytes(value: float) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.2f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def human_duration(seconds: float) -> str:
    if seconds < 0 or seconds == float("inf"):
        return "unknown"
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def allocated_bytes(path: Path) -> int:
    stat = path.stat()
    # Parallel downloads make the file logically full-sized up front. st_blocks
    # measures blocks actually written, which is the useful value for sparse files.
    blocks = getattr(stat, "st_blocks", None)
    return blocks * 512 if blocks is not None else stat.st_size


def metadata_progress(partial: Path) -> tuple[int, int] | None:
    metadata = partial.with_suffix(partial.suffix + ".json")
    if not metadata.exists():
        return None
    try:
        state = json.loads(metadata.read_text())
        ranges = [tuple(item) for item in state["ranges"]]
        completed = {tuple(item) for item in state["completed"]}
        prefix = min((start for start, _ in ranges), default=state["size"])
        downloaded = prefix + sum(end - start + 1 for start, end in completed)
        return int(state["size"]), min(int(downloaded), int(state["size"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        return None


def find_partial(directory: Path) -> Path:
    candidates = sorted(
        directory.glob("*.zim.part"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No .zim.part download found in {directory}")
    return candidates[0]


def monitor(partial: Path, interval: float, configured_total: int | None) -> None:
    final = partial.with_suffix("")
    previous_bytes = allocated_bytes(partial)
    previous_time = time.monotonic()
    smoothed_speed: float | None = None

    print(f"Monitoring {partial}")
    try:
        while True:
            time.sleep(interval)
            if not partial.exists():
                if final.exists():
                    print(f"Complete: {final} ({human_bytes(final.stat().st_size)})")
                    return
                raise FileNotFoundError(f"Download disappeared: {partial}")

            now = time.monotonic()
            progress = metadata_progress(partial)
            if progress:
                total, current_bytes = progress
            else:
                total = configured_total
                current_bytes = allocated_bytes(partial)
            elapsed = max(now - previous_time, 0.001)
            instantaneous = max(0, current_bytes - previous_bytes) / elapsed
            smoothed_speed = (
                instantaneous
                if smoothed_speed is None
                else 0.3 * instantaneous + 0.7 * smoothed_speed
            )
            status = f"{human_bytes(current_bytes)} downloaded"
            if total:
                percent = min(100.0, current_bytes * 100 / total)
                remaining = max(0, total - current_bytes)
                eta = remaining / smoothed_speed if smoothed_speed else float("inf")
                status += f" / {human_bytes(total)} ({percent:.1f}%)"
                status += f" | ETA {human_duration(eta)}"
            status += f" | {human_bytes(smoothed_speed)}/s"
            print(f"\r{status:<90}", end="", flush=True)
            previous_bytes = current_bytes
            previous_time = now
    except KeyboardInterrupt:
        print("\nStopped monitoring; the download is unaffected.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        help=".zim.part file (defaults to the newest file in data/kiwix)",
    )
    parser.add_argument(
        "--interval", type=float, default=5.0, help="sampling interval in seconds"
    )
    parser.add_argument(
        "--total-gib",
        type=float,
        help="expected size when parallel-download metadata is unavailable",
    )
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be greater than zero")

    partial = args.path or find_partial(Path("data/kiwix"))
    partial = partial.expanduser().resolve()
    if not partial.exists():
        parser.error(f"file does not exist: {partial}")
    total = int(args.total_gib * 1024**3) if args.total_gib else None
    monitor(partial, args.interval, total)


if __name__ == "__main__":
    main()
