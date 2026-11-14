"""Regression: the run tracer must receive the question, not the answer.

W5 called ``WorkerPool._trace(run_id, answer, ...)`` on the success path. The
tracer derives the Langfuse *input* from ``getattr(obj, "question", "")``, and
``Answer`` has no ``question`` attribute — so every successful run was recorded
with an empty input while the output was present. This test pins the caller
contract at the seam where the bug lived.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from api.worker import WorkerPool
from eval.schema import Answer, Question


class _Store:
    def __init__(self) -> None:
        self.finished: list[tuple[Any, ...]] = []

    async def finish(self, run_id: Any, payload: Any, usage: Any) -> None:
        self.finished.append((run_id, payload, usage))


class _Bus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, *args: Any, **kwargs: Any) -> None:
        self.published.append((args, kwargs))


class _Tracer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_finish_traces_the_question_not_the_answer() -> None:
    tracer = _Tracer()
    pool = WorkerPool(_Store(), _Bus(), agent=object(), tracer=tracer)
    question = Question(
        id="adhoc",
        category="fact",
        question="苹果 FY2024 的总净营收是多少美元？",
        as_of=date(2024, 12, 31),
        universe=["AAPL"],
    )
    answer = Answer(text="391,035 百万美元")

    await pool._finish(str(uuid4()), question, answer)

    assert tracer.calls, "finish must emit exactly one Langfuse span"
    assert tracer.calls[0]["question"] == question.question
    assert tracer.calls[0]["answer"] is not None
