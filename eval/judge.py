"""Rule-based judge: a pipeline stub, not a correctness oracle.

It can decide refusal behaviour deterministically, which is exactly what W1
needs (the only agent is the null agent). Semantic grading arrives in W3 with an
LLM judge calibrated against human labels; until then the report must say
``judge=rule-stub`` so nobody mistakes these numbers for answer quality.
"""

from __future__ import annotations

from eval.schema import Answer, Grade, Question


class RuleJudge:
    name = "rule-stub"

    async def grade(self, question: Question, answer: Answer) -> Grade:
        if question.must_refuse:
            if answer.refused:
                return Grade(
                    correctness=None,
                    citation_support=1.0,
                    notes="refused as required",
                )
            return Grade(
                correctness=0,
                citation_support=0.0,
                hallucination=True,
                notes="answered an unanswerable question",
            )

        if answer.refused:
            return Grade(
                correctness=0,
                citation_support=0.0,
                notes="false refusal",
            )

        # Non-refusal answers cannot be graded without semantic judging.
        supported = 1.0 if answer.citations and _citations_well_formed(answer) else 0.0
        return Grade(
            correctness=0,
            citation_support=supported,
            numeric_ok=None,
            notes="unverified by rule-stub judge",
        )


def _citations_well_formed(answer: Answer) -> bool:
    return all(c.char_end > c.char_start >= 0 and c.quote for c in answer.citations)
