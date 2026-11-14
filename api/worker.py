"""Bounded queue + worker pool around ``GraphAgent``.

Why a queue instead of "just await the agent in the request": a run takes 13 s
and can take 120 s under provider jitter. Twenty concurrent requests must not
spawn twenty unbounded tasks; they must wait, and the queue depth must be
visible in ``/api/health``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from api.db import RunStore
from api.events import EventBus
from eval.schema import Answer, Question

log = logging.getLogger("fintrace.api.worker")


class _Agent(Protocol):
    async def astream_answer(self, question: Question, on_event: Any = None) -> Answer: ...
    async def start_review(self, question: Question) -> dict[str, Any]: ...


class WorkerPool:
    def __init__(
        self,
        store: RunStore,
        bus: EventBus,
        agent: _Agent,
        review_agent: _Agent | None = None,
        *,
        workers: int = 6,
        queue_size: int = 50,
        tracer: Any | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._agent = agent
        self._review_agent = review_agent or agent
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self._workers = workers
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = asyncio.Event()
        self._tracer = tracer

    @property
    def review_agent(self) -> _Agent:
        return self._review_agent

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def capacity(self) -> int:
        return self._queue.maxsize

    def full(self) -> bool:
        return self._queue.full()

    def submit_nowait(self, run_id: str) -> None:
        self._queue.put_nowait(run_id)

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop(i), name=f"run-worker-{i}") for i in range(self._workers)
        ]

    async def stop(self) -> None:
        self._closing.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _loop(self, index: int) -> None:
        while not self._closing.is_set():
            run_id = await self._queue.get()
            try:
                await self._process(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                log.exception("worker %s crashed on %s", index, run_id)
                await self._store.fail(_uuid(run_id), "worker crashed")
                self._bus.publish(run_id, {"type": "failed", "error": "worker crashed"})
            finally:
                self._queue.task_done()

    def _trace(
        self,
        run_id: str,
        question_or_answer: Any,
        answer: dict[str, Any] | None,
        usage: dict[str, Any] | None,
        status: str,
        error: str | None,
    ) -> None:
        if self._tracer is None:
            return
        question = getattr(question_or_answer, "question", "") or ""
        self._tracer.record(
            run_id=run_id,
            question=str(question),
            answer=answer,
            usage=usage,
            status=status,
            latency_ms=int((usage or {}).get("latency_ms") or 0),
            error=error,
        )

    async def _process(self, run_id: str) -> None:
        row = await self._store.get(_uuid(run_id))
        if row is None or row["status"] != "queued":
            return
        await self._store.mark_running(_uuid(run_id))
        self._bus.publish(run_id, {"type": "running"})
        question = Question.model_validate(row["question"])

        if row["review"]:
            outcome = await self._review_agent.start_review(question)
            if outcome.get("status") == "awaiting_review":
                await self._store.await_review(
                    _uuid(run_id), str(outcome["thread_id"]), outcome.get("payload") or {}
                )
                self._bus.publish(
                    run_id,
                    {
                        "type": "awaiting_review",
                        "thread_id": outcome["thread_id"],
                        "payload": outcome.get("payload"),
                    },
                )
                return
            answer = outcome.get("answer")
            if answer is None:
                await self._store.fail(_uuid(run_id), "review run produced no answer")
                self._trace(run_id, question, None, None, "failed", "no answer")
                self._bus.publish(run_id, {"type": "failed", "error": "no answer"})
                return
            await self._finish(run_id, question, answer)
            return

        async def on_event(node: str, _update: dict[str, Any]) -> None:
            self._bus.publish(run_id, {"type": "node", "node": node})

        answer = await self._agent.astream_answer(question, on_event)
        await self._finish(run_id, question, answer)

    async def _finish(self, run_id: str, question: Question, answer: Answer) -> None:
        payload = answer.model_dump(mode="json")
        usage = answer.usage.model_dump(mode="json")
        await self._store.finish(_uuid(run_id), payload, usage)
        # The tracer derives the Langfuse *input* from the question. Passing the
        # Answer here (as W5 did) yields an empty input: Answer has no .question.
        self._trace(run_id, question, payload, usage, "done", None)
        self._bus.publish(
            run_id,
            {
                "type": "done",
                "answer": payload,
                "usage": answer.usage.model_dump(mode="json"),
            },
        )


def _uuid(value: str) -> Any:
    from uuid import UUID

    return UUID(value)
