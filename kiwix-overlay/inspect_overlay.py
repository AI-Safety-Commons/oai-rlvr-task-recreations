"""Read-only AISI Inspect views for Kiwix Overlay's HTML recent-changes page."""

from __future__ import annotations

import asyncio
import json
import os
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import urlopen

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.log import transcript
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.viewer import TaskSamplesColumn, TaskSamplesSort, TaskSamplesView, ViewerConfig

DEFAULT_URL = os.environ.get("KIWIX_OVERLAY_URL", "http://localhost:3010")


class ChangesParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_body = self.in_cell = False
        self.cell = ""
        self.row: list[str] = []
        self.changes: list[dict] = []

    def handle_starttag(self, tag, _attrs):
        if tag == "tbody":
            self.in_body = True
        elif self.in_body and tag == "td":
            self.in_cell, self.cell = True, ""

    def handle_endtag(self, tag):
        if tag == "tbody":
            self.in_body = False
        elif self.in_body and tag == "td":
            self.in_cell = False
            self.row.append(self.cell.strip())
        elif self.in_body and tag == "tr":
            if len(self.row) == 5 and self.row[0].startswith("#"):
                self.changes.append({"revision": int(self.row[0][1:]), "created_at": self.row[1], "title": self.row[2], "username": self.row[3], "summary": self.row[4]})
            self.row = []

    def handle_data(self, data):
        if self.in_cell:
            self.cell += data


def read_changes(base_url: str) -> list[dict]:
    with urlopen(f"{base_url.rstrip('/')}/changes", timeout=10) as response:
        parser = ChangesParser()
        parser.feed(response.read().decode())
        return parser.changes


def export_changes(changes: list[dict], output_file: str) -> Path:
    destination = Path(output_file).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    temporary.write_text(json.dumps(changes, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(destination)
    return destination


@solver
def keep_sample() -> Solver:
    async def solve(state: TaskState, _generate: Generate) -> TaskState:
        return state
    return solve


@solver
def stream_changes(base_url: str, poll_seconds: float, duration_seconds: int, output_file: str) -> Solver:
    async def solve(state: TaskState, _generate: Generate) -> TaskState:
        seen = 0
        deadline = asyncio.get_running_loop().time() + duration_seconds
        transcript().info("Watching Kiwix Overlay edits", source="kiwix-overlay")
        while asyncio.get_running_loop().time() < deadline:
            for change in reversed(await asyncio.to_thread(read_changes, base_url)):
                if change["revision"] > seen:
                    transcript().info(change, source="kiwix-overlay")
                    seen = max(seen, change["revision"])
            await asyncio.sleep(poll_seconds)
        changes = await asyncio.to_thread(read_changes, base_url)
        destination = await asyncio.to_thread(export_changes, changes, output_file)
        transcript().info(f"Exported {len(changes)} edits to {destination}", source="kiwix-overlay")
        return state
    return solve


@task
def overlay_snapshot(base_url: str = DEFAULT_URL, output_file: str = "overlay-changes.json") -> Task:
    changes = read_changes(base_url)
    export_changes(changes, output_file)
    samples = [Sample(id=item["revision"], input=f"{item['title']}: {item['summary']}", target=item["username"], metadata={"change": item}) for item in changes]
    if not samples:
        samples = [Sample(id=0, input="No overlay edits", target="")]
    return Task(
        dataset=MemoryDataset(samples), solver=keep_sample(),
        viewer=ViewerConfig(task_samples_view=TaskSamplesView(name="Overlay edits", columns=[TaskSamplesColumn(id="sampleId"), TaskSamplesColumn(id="target"), TaskSamplesColumn(id="input")], sort=[TaskSamplesSort(column="sampleId", dir="desc")], multiline=True)),
    )


@task
def overlay_live(base_url: str = DEFAULT_URL, poll_seconds: float = 1.0, duration_seconds: int = 3600, output_file: str = "overlay-changes.json") -> Task:
    return Task(dataset=[Sample(id="live", input="Live Kiwix Overlay edits")], solver=stream_changes(base_url, poll_seconds, duration_seconds, output_file), time_limit=duration_seconds + 10)

