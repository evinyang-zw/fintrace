"""Rule-based judge: a pipeline stub, not a correctness oracle.

It can decide refusal behaviour deterministically, which is exactly what W1
needs (the only agent is the null agent). Semantic grading arrives in W3 with an
LLM judge calibrated against human labels; until then the report must say
``judge=rule-stub`` so nobody mistakes these numbers for answer quality.
"""

from __future__ import annotations

from eval.citation import CitationChecker
from eval.graders import DeterministicJudge
from eval.llm_judge import LLMJudge
from eval.schema import Answer, Grade, Question
from pipelines.config import Settings


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


class W3Judge:
    """Correctness from the LLM judge, citation support from the span checker.

    The deterministic judge still runs, but only as a cross-check: it is cheap,
    it catches "right number, wrong conclusion", and a disagreement between the
    two is written into the notes instead of being averaged away.
    """

    name = "llm-v1"

    def __init__(
        self,
        settings: Settings,
        *,
        llm_judge: LLMJudge | None = None,
        deterministic: DeterministicJudge | None = None,
        citation_checker: CitationChecker | None = None,
    ) -> None:
        self._llm = llm_judge or LLMJudge(settings)
        self._det = deterministic or DeterministicJudge(settings)
        self._citation = citation_checker or CitationChecker(settings)

    async def grade(self, question: Question, answer: Answer) -> Grade:
        det = await self._det.grade(question, answer)
        if question.must_refuse:
            return det
        if answer.refused:
            # W3 scores every answerable question, including multi-hop. A refusal
            # there is a failure, not an abstention.
            return Grade(correctness=0, citation_support=0.0, notes="false refusal")

        judged = await self._llm.grade(question, answer)
        cited = await self._citation.check(question, answer)
        notes = [judged.notes, cited.as_note(), det.notes]
        if judged.correctness is not None and det.numeric_ok is False and judged.correctness >= 2:
            notes.append("det_judge_disagree: numeric miss but judge says correct")
        if judged.correctness is not None and det.numeric_ok is True and judged.correctness == 0:
            notes.append("det_judge_disagree: numeric hit but judge says wrong")
        return Grade(
            correctness=judged.correctness,
            citation_support=cited.score,
            numeric_ok=det.numeric_ok,
            notes="; ".join(part for part in notes if part),
        )
