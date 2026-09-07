"""Read-only Inspect views over activity created in the clone."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.request import urlopen

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.log import transcript
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.viewer import (
    TaskSamplesColumn,
    TaskSamplesSort,
    TaskSamplesView,
    ViewerConfig,
)

DEFAULT_URL = os.environ.get("STACKEXCHANGE_URL", "http://localhost:3020")


def events(base_url: str, after: int = 0) -> list[dict]:
    found: list[dict] = []
    cursor = after
    while True:
        with urlopen(
            f"{base_url.rstrip('/')}/api/events?after={cursor}", timeout=10
        ) as response:
            page = json.load(response)["events"]
        found.extend(page)
        if len(page) < 1000:
            return found
        cursor = page[-1]["id"]


def export(items: list[dict], output_file: str) -> Path:
    destination = Path(output_file).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(destination)
    return destination


@solver
def keep_sample() -> Solver:
    async def solve(state: TaskState, _generate: Generate) -> TaskState:
        return state

    return solve


@solver
def stream(
    base_url: str, poll_seconds: float, duration_seconds: int, output_file: str
) -> Solver:
    async def solve(state: TaskState, _generate: Generate) -> TaskState:
        seen = 0
        deadline = asyncio.get_running_loop().time() + duration_seconds
        transcript().info(
            "Watching Stack Exchange clone activity", source="stackexchange-clone"
        )
        while asyncio.get_running_loop().time() < deadline:
            for item in await asyncio.to_thread(events, base_url, seen):
                seen = max(seen, item["id"])
                transcript().info(item, source="stackexchange-clone")
            await asyncio.sleep(poll_seconds)
        items = await asyncio.to_thread(events, base_url, 0)
        await asyncio.to_thread(export, items, output_file)
        return state

    return solve


def viewer() -> ViewerConfig:
    return ViewerConfig(
        task_samples_view=TaskSamplesView(
            name="Agent activity",
            columns=[
                TaskSamplesColumn(id="sampleId"),
                TaskSamplesColumn(id="target"),
                TaskSamplesColumn(id="input"),
            ],
            sort=[TaskSamplesSort(column="sampleId", dir="desc")],
            multiline=True,
        )
    )


@task
def stackexchange_snapshot(
    base_url: str = DEFAULT_URL, output_file: str = "agent-activity.json"
) -> Task:
    items = events(base_url)
    export(items, output_file)
    samples = [
        Sample(
            id=x["id"],
            input=x["summary"],
            target=f"{x['actor']} · {x['kind']} · {x['site_slug']}",
            metadata={"event": x},
        )
        for x in items
    ]
    return Task(
        dataset=MemoryDataset(
            samples or [Sample(id=0, input="No activity", target="")]
        ),
        solver=keep_sample(),
        viewer=viewer(),
    )


@task
def stackexchange_live(
    base_url: str = DEFAULT_URL,
    poll_seconds: float = 1.0,
    duration_seconds: int = 3600,
    output_file: str = "agent-activity.json",
) -> Task:
    return Task(
        dataset=[Sample(id="live", input="Live Stack Exchange clone activity")],
        solver=stream(base_url, poll_seconds, duration_seconds, output_file),
        time_limit=duration_seconds + 10,
    )
