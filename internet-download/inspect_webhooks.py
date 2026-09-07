"""Inspect views for the fake webhooks.com event stream."""

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

DEFAULT_URL = os.environ.get("WEBHOOKS_URL", "http://webhooks.com")


def events(base_url: str, after: int = 0) -> list[dict]:
    with urlopen(f"{base_url.rstrip('/')}/api/events?after={after}", timeout=10) as response:
        return json.load(response)["events"]


def export(items: list[dict], output_file: str) -> Path:
    destination = Path(output_file).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    return destination


@solver
def keep_sample() -> Solver:
    async def solve(state: TaskState, _generate: Generate) -> TaskState:
        return state

    return solve


@solver
def stream(base_url: str, poll_seconds: float, duration_seconds: int, output_file: str) -> Solver:
    async def solve(state: TaskState, _generate: Generate) -> TaskState:
        seen = 0
        deadline = asyncio.get_running_loop().time() + duration_seconds
        transcript().info("Watching webhooks.com activity", source="webhooks.com")
        while asyncio.get_running_loop().time() < deadline:
            for item in await asyncio.to_thread(events, base_url, seen):
                seen = max(seen, item["id"])
                transcript().info(item, source="webhooks.com")
            await asyncio.sleep(poll_seconds)
        export(await asyncio.to_thread(events, base_url), output_file)
        return state

    return solve


@task
def webhooks_snapshot(base_url: str = DEFAULT_URL, output_file: str = "webhook-activity.json") -> Task:
    items = events(base_url)
    export(items, output_file)
    return Task(
        dataset=MemoryDataset(
            [
                Sample(
                    id=item["id"],
                    input=json.dumps(item["json"]),
                    target=item["path"],
                    metadata={"event": item},
                )
                for item in items
            ]
            or [Sample(id=0, input="No webhook activity", target="")]
        ),
        solver=keep_sample(),
    )


@task
def webhooks_live(
    base_url: str = DEFAULT_URL,
    poll_seconds: float = 1.0,
    duration_seconds: int = 3600,
    output_file: str = "webhook-activity.json",
) -> Task:
    return Task(
        dataset=[Sample(id="live", input="Live webhooks.com activity")],
        solver=stream(base_url, poll_seconds, duration_seconds, output_file),
        time_limit=duration_seconds + 10,
    )
