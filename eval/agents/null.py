"""The deliberately stupid agent.

Its job is to validate the measuring instrument, not to answer questions: if the
null agent's report does not come out as a set of explainable zeros, the bug is
in ``eval/``.
"""

from __future__ import annotations

from eval.schema import Answer, Question


class NullAgent:
    name = "null"

    async def answer(self, question: Question) -> Answer:
        return Answer(
            text="无法从现有证据中确认。",
            citations=[],
            refused=True,
            refusal_reason="no_retrieval",
            trace_id=f"null-{question.id}",
        )
