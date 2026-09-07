from __future__ import annotations

import argparse
import time
from pathlib import Path

from .commoncrawl import (
    DEFAULT_CONTACT_EMAIL,
    columnar_index_paths,
    discover_columnar,
    download_plan,
    write_plan,
)
from .config import load_config, resolve_output
from .dataset_search import (
    extract_dataset_manifest,
    extract_overlay_database,
    initialize_overlay_cursor,
    initialize_webhooks_cursor,
    sync_overlay,
    sync_webhooks,
)
from .datasets import fetch_dataset_plan, write_dataset_plan
from .extract import extract_manifest
from .federated import query_federated, serve_search
from .kiwix import download_archive
from .packages import fetch_alpine, fetch_cargo, fetch_go, fetch_npm, fetch_pypi
from .search import build_index, sync_schelling_point
from .targets import load_targets, merge_targets


def commoncrawl_targets(config_path: Path, config: dict) -> list[dict]:
    ranked: list[dict] = []
    if targets_file := config.get("targets_file"):
        ranked = load_targets(resolve_output(config_path, targets_file))
    targets = merge_targets(ranked, list(config.get("targets", [])))
    if max_per_target := config.get("max_records_per_target"):
        limit = int(max_per_target)
        if limit <= 0:
            raise ValueError("commoncrawl.max_records_per_target must be positive")
        targets = [
            {
                **target,
                "max_records": min(int(target.get("max_records", 10_000)), limit),
            }
            for target in targets
        ]
    return targets


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def plan_commoncrawl(config_path: Path) -> None:
    config = load_config(config_path)["commoncrawl"]
    output = resolve_output(config_path, config.get("output", "data/commoncrawl"))
    targets = commoncrawl_targets(config_path, config)
    cache_dir = output / "index-cache" / config["crawl"]
    contact_email = config.get("contact_email", DEFAULT_CONTACT_EMAIL)
    paths = columnar_index_paths(
        config["crawl"], cache_dir, contact_email=contact_email
    )
    captures = discover_columnar(
        paths,
        targets,
        database=cache_dir / "planning.duckdb",
        threads=int(config.get("index_threads", 4)),
        contact_email=contact_email,
    )
    count, size = write_plan(captures, output / "plan.jsonl")
    print(
        f"Planned {count:,} unique records from {len(targets):,} domains "
        f"({human_bytes(size)} compressed)"
    )


def fetch_commoncrawl(config_path: Path) -> None:
    config = load_config(config_path)["commoncrawl"]
    output = resolve_output(config_path, config.get("output", "data/commoncrawl"))
    count, size = download_plan(
        output / "plan.jsonl",
        output,
        workers=int(config.get("workers", 8)),
        max_total_bytes=config.get("max_total_bytes"),
        request_interval_seconds=float(
            config.get("download_request_interval_seconds", 1.0)
        ),
        request_rate_per_second=(
            float(config["download_request_rate_per_second"])
            if "download_request_rate_per_second" in config
            else None
        ),
        contact_email=config.get("contact_email", DEFAULT_CONTACT_EMAIL),
    )
    print(f"Downloaded {count:,} records ({human_bytes(size)})")


def fetch_kiwix(config_path: Path) -> None:
    config = load_config(config_path)["kiwix"]
    output = resolve_output(config_path, config.get("output", "data/kiwix"))
    workers = int(config.get("workers", 4))
    for archive in config.get("archives", []):
        destination = download_archive(archive, output, workers=workers)
        print(f"Downloaded {archive['name']}: {destination}")


def fetch_package_manager(config_path: Path, manager: str) -> None:
    packages = load_config(config_path).get("packages", {})
    config = packages.get(manager)
    if config is None:
        raise ValueError(f"[packages.{manager}] is not configured")
    output = resolve_output(
        config_path, config.get("output", f"data/packages/{manager}")
    )
    fetchers = {
        "alpine": fetch_alpine,
        "pypi": fetch_pypi,
        "cargo": fetch_cargo,
        "npm": fetch_npm,
        "go": fetch_go,
    }
    fetchers[manager](config, output)
    print(f"Fetched {manager} packages into {output}")


def fetch_packages(config_path: Path) -> None:
    for manager in ("alpine", "pypi", "cargo", "npm", "go"):
        fetch_package_manager(config_path, manager)


def dataset_paths(config_path: Path) -> tuple[dict, Path]:
    config = load_config(config_path).get("datasets", {})
    output = resolve_output(config_path, config.get("output", "data/datasets"))
    return config, output


def plan_datasets(config_path: Path) -> None:
    config, output = dataset_paths(config_path)
    count, providers = write_dataset_plan(config, output)
    summary = ", ".join(f"{key}={value:,}" for key, value in sorted(providers.items()))
    print(f"Planned {count:,} dataset files ({summary}) into {output / 'plan.jsonl'}")


def fetch_datasets(config_path: Path, providers: list[str] | None = None) -> None:
    config, output = dataset_paths(config_path)
    totals = fetch_dataset_plan(
        output / "plan.jsonl",
        output,
        max_total_bytes=(
            int(config["max_total_bytes"]) if "max_total_bytes" in config else None
        ),
        max_file_bytes=(
            int(config["max_file_bytes"]) if "max_file_bytes" in config else None
        ),
        providers=providers or (),
    )
    print(
        f"Dataset download: {totals['downloaded']:,} downloaded, "
        f"{totals['existing']:,} reused, {totals['skipped']:,} skipped/errors, "
        f"{human_bytes(totals['bytes'])} transferred"
    )


def download_all(config_path: Path) -> None:
    config = load_config(config_path)["commoncrawl"]
    output = resolve_output(config_path, config.get("output", "data/commoncrawl"))
    if (output / "plan.jsonl").exists():
        print("Reusing existing Common Crawl plan.jsonl")
    else:
        plan_commoncrawl(config_path)
    fetch_commoncrawl(config_path)
    fetch_kiwix(config_path)
    dataset_config, dataset_output = dataset_paths(config_path)
    if dataset_config:
        if not (dataset_output / "plan.jsonl").exists():
            plan_datasets(config_path)
        fetch_datasets(config_path)
    fetch_packages(config_path)


def search_paths(config_path: Path) -> tuple[Path, Path, Path]:
    root = load_config(config_path)
    commoncrawl = root["commoncrawl"]
    search = root.get("search", {})
    commoncrawl_output = resolve_output(
        config_path, commoncrawl.get("output", "data/commoncrawl")
    )
    documents = resolve_output(
        config_path, search.get("documents", "data/search/documents.jsonl")
    )
    database = resolve_output(
        config_path, search.get("database", "data/search/search.sqlite3")
    )
    return commoncrawl_output / "manifest.jsonl", documents, database


def extract_commoncrawl(config_path: Path) -> None:
    root = load_config(config_path)
    manifest, documents, _ = search_paths(config_path)
    max_chars = int(root.get("search", {}).get("max_chars_per_document", 250_000))
    count, skipped = extract_manifest(manifest, documents, max_chars=max_chars)
    print(f"Extracted {count:,} text documents; skipped {skipped:,} non-text records")


def dataset_search_paths(config_path: Path) -> tuple[Path, Path]:
    root = load_config(config_path)
    _, output = dataset_paths(config_path)
    search = root.get("search", {})
    documents = resolve_output(
        config_path, search.get("dataset_documents", "data/search/datasets.jsonl")
    )
    return output / "manifest.jsonl", documents


def extract_datasets(config_path: Path) -> None:
    root = load_config(config_path)
    manifest, documents = dataset_search_paths(config_path)
    search = root.get("search", {})
    count, skipped = extract_dataset_manifest(
        manifest,
        documents,
        max_chars=int(search.get("max_chars_per_document", 250_000)),
        rows_per_document=int(search.get("dataset_rows_per_document", 100)),
    )
    print(f"Extracted {count:,} dataset documents; skipped {skipped:,} files")


def overlay_search_paths(config_path: Path) -> tuple[Path, Path]:
    root = load_config(config_path)
    search = root.get("search", {})
    database = resolve_output(
        config_path,
        search.get("overlay_database", "../kiwix-overlay/data/overlay.db"),
    )
    documents = resolve_output(
        config_path, search.get("overlay_documents", "data/search/overlay.jsonl")
    )
    return database, documents


def overlay_cursor_path(config_path: Path) -> Path:
    root = load_config(config_path)
    return resolve_output(
        config_path,
        root.get("search", {}).get("overlay_cursor", "data/search/overlay.cursor"),
    )


def webhooks_paths(config_path: Path) -> tuple[Path, Path]:
    root = load_config(config_path)
    search = root.get("search", {})
    events = resolve_output(config_path, search.get("webhooks_events", "../data/webhooks/events.jsonl"))
    cursor = resolve_output(config_path, search.get("webhooks_cursor", "data/search/webhooks.cursor"))
    return events, cursor


def extract_overlay(config_path: Path) -> None:
    root = load_config(config_path)
    database, documents = overlay_search_paths(config_path)
    count, skipped = extract_overlay_database(
        database,
        documents,
        max_chars=int(root.get("search", {}).get("max_chars_per_document", 250_000)),
    )
    print(f"Extracted {count:,} Kiwix overlay documents; skipped {skipped:,} revisions")


def build_search(config_path: Path) -> None:
    _, documents, database = search_paths(config_path)
    _, dataset_documents = dataset_search_paths(config_path)
    search = load_config(config_path).get("search", {})
    paths = [documents, dataset_documents]
    if search.get("include_overlay_history", False):
        paths.append(overlay_search_paths(config_path)[1])
    count = build_index(paths, database, search.get("source_tags", {}))
    print(f"Indexed {count:,} documents into {database}")


def prepare_search(config_path: Path) -> None:
    extract_commoncrawl(config_path)
    manifest, _ = dataset_search_paths(config_path)
    if manifest.exists():
        extract_datasets(config_path)
    build_search(config_path)
    root = load_config(config_path)
    messages = root.get("search", {}).get("schelling_point_messages")
    if messages:
        messages_path = resolve_output(config_path, messages)
        if messages_path.exists():
            sync_messages(config_path, messages_path)
    overlay_database, _ = overlay_search_paths(config_path)
    watermark = initialize_overlay_cursor(overlay_database, overlay_cursor_path(config_path))
    print(f"Kiwix overlay baseline: revision {watermark:,}; later edits use sync-overlay")
    events, cursor = webhooks_paths(config_path)
    if events.exists():
        watermark = initialize_webhooks_cursor(events, cursor)
        print(f"Webhooks baseline: event {watermark:,}; later events use watch-webhooks")


def sync_schelling_live(config_path: Path) -> None:
    messages = schelling_messages_path(config_path)
    tags = load_config(config_path).get("search", {}).get("source_tags", {}).get("schelling-point", [])
    added, updated, deleted = sync_schelling_point(
        search_paths(config_path)[2], messages, tags=tags
    )
    print(f"Schelling Point sync: {added:,} added, {updated:,} updated, {deleted:,} deleted")


def sync_overlay_command(config_path: Path) -> None:
    overlay_database, _ = overlay_search_paths(config_path)
    tags = load_config(config_path).get("search", {}).get("source_tags", {}).get("kiwix-overlay", [])
    added, updated = sync_overlay(
        overlay_database, search_paths(config_path)[2], overlay_cursor_path(config_path), tags
    )
    print(f"Kiwix overlay sync: {added:,} added, {updated:,} updated")


def watch_overlay(config_path: Path, interval: float) -> None:
    try:
        while True:
            sync_overlay_command(config_path)
            time.sleep(max(0.1, interval))
    except KeyboardInterrupt:
        print("Stopped Kiwix overlay search watcher")


def sync_webhooks_command(config_path: Path) -> None:
    events, cursor = webhooks_paths(config_path)
    tags = load_config(config_path).get("search", {}).get("source_tags", {}).get("webhooks.com", [])
    added, updated = sync_webhooks(events, search_paths(config_path)[2], cursor, tags)
    print(f"Webhooks live sync: {added:,} added, {updated:,} updated")


def watch_webhooks(config_path: Path, interval: float) -> None:
    try:
        while True:
            sync_webhooks_command(config_path)
            time.sleep(max(0.1, interval))
    except KeyboardInterrupt:
        print("Stopped webhooks search watcher")


def print_search(config_path: Path, query: str, limit: int) -> None:
    _, _, database = search_paths(config_path)
    search = load_config(config_path).get("search", {})
    kiwix_url = search.get("kiwix_url")
    for index, result in enumerate(
        query_federated(database, query, limit, kiwix_url, search.get("source_boosts", {})), start=1
    ):
        print(f"{index}. {result['title'] or result['url']}")
        print(f"   {result['url']}")
        print(f"   [{result['source']}] {result['snippet']}")


def run_search_server(
    config_path: Path, host: str, port: int, kiwix_url: str | None
) -> None:
    _, _, database = search_paths(config_path)
    search = load_config(config_path).get("search", {})
    serve_search(
        database,
        host=host,
        port=port,
        kiwix_url=kiwix_url or search.get("kiwix_url"),
    )


def schelling_messages_path(config_path: Path, override: Path | None = None) -> Path:
    if override is not None:
        return override
    search = load_config(config_path).get("search", {})
    configured = search.get("schelling_point_messages")
    if not configured:
        raise ValueError("search.schelling_point_messages is not configured")
    return resolve_output(config_path, configured)


def sync_messages(config_path: Path, override: Path | None = None) -> None:
    _, _, database = search_paths(config_path)
    messages = schelling_messages_path(config_path, override)
    added, updated, deleted = sync_schelling_point(database, messages)
    print(
        f"Schelling Point search sync: {added} added, "
        f"{updated} updated, {deleted} removed"
    )


def watch_messages(config_path: Path, override: Path | None, interval: float) -> None:
    messages = schelling_messages_path(config_path, override)
    last_signature: tuple[int, int] | None = None
    try:
        while True:
            stat = messages.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature != last_signature:
                if override is None:
                    sync_schelling_live(config_path)
                else:
                    sync_messages(config_path, messages)
                last_signature = signature
            time.sleep(interval)
    except KeyboardInterrupt:
        print("Stopped Schelling Point search watcher")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="internet-download")
    result.add_argument("--config", type=Path, default=Path("config.toml"))
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("plan-commoncrawl")
    commands.add_parser("fetch-commoncrawl")
    commands.add_parser("fetch-kiwix")
    commands.add_parser("fetch-alpine")
    commands.add_parser("fetch-pypi")
    commands.add_parser("fetch-cargo")
    commands.add_parser("fetch-npm")
    commands.add_parser("fetch-go")
    commands.add_parser("fetch-packages")
    commands.add_parser("plan-datasets")
    fetch_dataset_command = commands.add_parser("fetch-datasets")
    fetch_dataset_command.add_argument("--provider", action="append")
    commands.add_parser("download-all")
    commands.add_parser("extract-commoncrawl")
    commands.add_parser("extract-datasets")
    commands.add_parser("extract-overlay")
    commands.add_parser("sync-overlay")
    watch_overlay_command = commands.add_parser("watch-overlay")
    watch_overlay_command.add_argument("--interval", type=float, default=1.0)
    commands.add_parser("sync-webhooks")
    watch_webhooks_command = commands.add_parser("watch-webhooks")
    watch_webhooks_command.add_argument("--interval", type=float, default=1.0)
    commands.add_parser("build-search")
    commands.add_parser("prepare-search")
    search_command = commands.add_parser("search")
    search_command.add_argument("query")
    search_command.add_argument("--limit", type=int, default=10)
    serve_command = commands.add_parser("serve-search")
    serve_command.add_argument("--host", default="127.0.0.1")
    serve_command.add_argument("--port", type=int, default=8091)
    serve_command.add_argument("--kiwix-url")
    sync_command = commands.add_parser("sync-schelling-point")
    sync_command.add_argument("--messages", type=Path)
    watch_command = commands.add_parser("watch-schelling-point")
    watch_command.add_argument("--messages", type=Path)
    watch_command.add_argument("--interval", type=float, default=1.0)
    return result


def main() -> None:
    args = parser().parse_args()
    commands = {
        "plan-commoncrawl": plan_commoncrawl,
        "fetch-commoncrawl": fetch_commoncrawl,
        "fetch-kiwix": fetch_kiwix,
        "fetch-alpine": lambda path: fetch_package_manager(path, "alpine"),
        "fetch-pypi": lambda path: fetch_package_manager(path, "pypi"),
        "fetch-cargo": lambda path: fetch_package_manager(path, "cargo"),
        "fetch-npm": lambda path: fetch_package_manager(path, "npm"),
        "fetch-go": lambda path: fetch_package_manager(path, "go"),
        "fetch-packages": fetch_packages,
        "plan-datasets": plan_datasets,
        "download-all": download_all,
        "extract-commoncrawl": extract_commoncrawl,
        "extract-datasets": extract_datasets,
        "extract-overlay": extract_overlay,
        "build-search": build_search,
        "prepare-search": prepare_search,
        "sync-overlay": sync_overlay_command,
    }
    if args.command == "search":
        print_search(args.config, args.query, args.limit)
    elif args.command == "serve-search":
        run_search_server(args.config, args.host, args.port, args.kiwix_url)
    elif args.command == "sync-schelling-point":
        sync_schelling_live(args.config) if args.messages is None else sync_messages(args.config, args.messages)
    elif args.command == "watch-schelling-point":
        watch_messages(args.config, args.messages, args.interval)
    elif args.command == "watch-overlay":
        watch_overlay(args.config, args.interval)
    elif args.command == "sync-webhooks":
        sync_webhooks_command(args.config)
    elif args.command == "watch-webhooks":
        watch_webhooks(args.config, args.interval)
    elif args.command == "fetch-datasets":
        fetch_datasets(args.config, args.provider)
    else:
        commands[args.command](args.config)


if __name__ == "__main__":
    main()
