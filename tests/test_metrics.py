from __future__ import annotations

from datetime import date

from eval.metrics import ItemResult, aggregate
from eval.schema import Answer, Citation, Grade, Question


def question(qid: str, *, must_refuse: bool = False, category: str = "fact") -> Question:
    return Question(
        id=qid,
        category=category,  # type: ignore[arg-type]
        question="q?",
        as_of=date(2025, 1, 1),
        universe=["MSFT"],
        must_refuse=must_refuse,
    )


def citation() -> Citation:
    return Citation(accession="0001", item_code="7", char_start=0, char_end=10, quote="hello")


def test_refusal_metrics_on_unanswerable() -> None:
    results = [
        ItemResult(
            question("r1", must_refuse=True),
            answer=Answer(text="no", refused=True),
            grade=Grade(correctness=None, citation_support=1.0),
        ),
        ItemResult(
            question("r2", must_refuse=True),
            answer=Answer(text="42", refused=False),
            grade=Grade(correctness=0, hallucination=True),
        ),
    ]
    metrics = aggregate(results)
    assert metrics.refusal_recall == 0.5
    assert metrics.hallucination_rate == 0.5
    assert metrics.answerable == 0


def test_false_refusal_is_a_zero_not_an_abstention() -> None:
    results = [
        ItemResult(
            question("a1"), answer=Answer(text="no", refused=True), grade=Grade(correctness=0)
        ),
    ]
    metrics = aggregate(results)
    assert metrics.false_refusal_rate == 1.0
    assert metrics.correctness == 0.0
    assert metrics.answerable == 1


def test_citation_denominator_is_questions_not_claims() -> None:
    results = [
        ItemResult(
            question("a1"),
            answer=Answer(text="x", citations=[citation()]),
            grade=Grade(correctness=2, citation_support=1.0),
        ),
        ItemResult(
            question("a2"),
            answer=Answer(text="long answer without citation"),
            grade=Grade(correctness=2, citation_support=0.0),
        ),
    ]
    metrics = aggregate(results)
    assert metrics.answerable == 2
    assert metrics.citation_support == 0.5


def test_correctness_counts_score_of_one_or_more() -> None:
    results = [
        ItemResult(question("a1"), answer=Answer(text="x"), grade=Grade(correctness=1)),
        ItemResult(question("a2"), answer=Answer(text="y"), grade=Grade(correctness=0)),
    ]
    metrics = aggregate(results)
    assert metrics.correctness == 0.5


def test_numeric_accuracy_counts_refusal_as_wrong() -> None:
    results = [
        ItemResult(
            question("c1", category="calculation"),
            answer=Answer(text="ok"),
            grade=Grade(correctness=2, numeric_ok=True),
        ),
        ItemResult(
            question("c2", category="calculation"),
            answer=Answer(text="no", refused=True),
            grade=Grade(correctness=0, numeric_ok=None),
        ),
    ]
    metrics = aggregate(results)
    assert metrics.numeric_accuracy == 0.5


def test_empty_results_are_safe() -> None:
    metrics = aggregate([])
    assert metrics.n == 0
    assert metrics.correctness == 0.0
    assert metrics.latency_p95_ms == 0.0
