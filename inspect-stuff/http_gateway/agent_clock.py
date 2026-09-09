"""Publish LLM wait intervals to the agent's clock shim.

One publisher per sandbox, used from the gateway's single asyncio event loop.
Nested/overlapping calls pause once; cancellation and failures always resume.
"""

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path


class AgentClock:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.active = 0
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(mode=0o644, exist_ok=True)
            # Finish a previous publisher's interrupted pause on restart.
            self._publish(False)

    def _publish(self, paused: bool) -> None:
        if self.path is None:
            return
        with self.path.open("r+") as state:
            fcntl.flock(state, fcntl.LOCK_EX)
            fields = state.read().split()
            excluded, started = map(int, fields) if fields else (0, 0)
            now = time.monotonic_ns()
            if started:
                excluded += max(0, now - started)
            state.seek(0)
            state.write(f"{excluded} {now if paused else 0}\n")
            state.truncate()
            state.flush()

    @contextmanager
    def pause(self):
        if self.active == 0:
            self._publish(True)
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            if self.active == 0:
                self._publish(False)
