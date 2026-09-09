"""Import available archived bodies into the gateway's explicit page mapping."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit


def import_pages(archive):
    bases = {}
    for manifest in sorted((archive / "agent-logs").glob("*/manifest.json")):
        source = json.loads(manifest.read_text()).get("source", {})
        if source.get("base_url", "").endswith(("wiki.cgi", "wiki.pl")):
            bases[source["wiki_name"]] = source["base_url"]
    selected = {}
    skipped = 0
    for path in sorted((archive / "agent-logs").glob("*/revisions.jsonl")):
        with path.open() as stream:
            for line in stream:
                row = json.loads(line)
                body = row.get("body")
                if not isinstance(body, str):
                    continue
                url = row.get("source_url") or row.get("shellac_source_url")
                if not url and row.get("wiki") in bases:
                    url = bases[row["wiki"]] + "?" + quote(row["name"], safe="/")
                if not url or urlsplit(url).scheme not in ("http", "https"):
                    skipped += 1
                    continue
                timestamp = row.get("time") or row.get("write_date")
                try:
                    stamp = datetime.fromisoformat(timestamp).timestamp()
                except (ValueError, TypeError):
                    stamp = 0
                rank = (stamp, row.get("seq") or 0)
                if url not in selected or rank > selected[url][0]:
                    selected[url] = (rank, body)
    return {url: selected[url][1] for url in sorted(selected)}, skipped


def main():
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        default=project.parent / "notes/WikiAgentSwarmInvestigation",
    )
    parser.add_argument("--output", type=Path, default=project / "seed-data/pages.json")
    parser.add_argument(
        "--replace", action="store_true", help="Replace an existing output mapping"
    )
    args = parser.parse_args()
    if not (args.archive / "agent-logs").is_dir():
        parser.error("Archive must contain agent-logs/")
    pages, skipped = import_pages(args.archive)
    if not pages:
        parser.error("No archived pages with known URLs found")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w" if args.replace else "x") as output:
        json.dump(pages, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(
        f"Imported {len(pages)} URLs into {args.output}; skipped {skipped} bodies without known URLs."
    )


if __name__ == "__main__":
    main()
