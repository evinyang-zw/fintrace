"""In-process pub/sub for SSE progress.

Single-process on purpose: the queue and these buffers live in memory, which is
why the app must run with ``--workers 1``. See the runbook.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

TERMINAL_EVENTS = {"done", "failed", "awaiting_review"}


class EventBus:
    def __init__(self, max_buffer: int = 200) -> None:
        self._max = max_buffer
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._seq: dict[str, int] = {}

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        seq = self._seq.get(run_id, 0) + 1
        self._seq[run_id] = seq
        payload = {**event, "seq": seq}
        buffer = self._buffers.setdefault(run_id, [])
        buffer.append(payload)
        if len(buffer) > self._max:
            del buffer[: len(buffer) - self._max]
        for queue in list(self._subs.get(run_id, ())):
            queue.put_nowait(payload)

    async def stream(
        self, run_id: str, *, heartbeat_s: float = 15.0
    ) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs.setdefault(run_id, set()).add(queue)
        try:
            for event in list(self._buffers.get(run_id, [])):
                yield event
                if event["type"] in TERMINAL_EVENTS:
                    return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
                except TimeoutError:
                    yield {"type": "ping"}
                    continue
                yield event
                if event["type"] in TERMINAL_EVENTS:
                    return
        finally:
            self._subs.get(run_id, set()).discard(queue)

    def clear(self, run_id: str) -> None:
        self._buffers.pop(run_id, None)
        self._seq.pop(run_id, None)
