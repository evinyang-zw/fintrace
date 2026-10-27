"""Metric definitions, with the edge cases spelled out in code.

Two decisions worth remembering:
* citation support is divided by the number of *answerable questions*, never by
  the number of claims — otherwise an agent improves its score by writing less.
* an answer that refuses a question it should have answered is a hard zero, not
  a "not applicable".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from math import ceil, floor

from eval.schema import Answer, Category, Grade, Question

NUMERIC_CATEGORIES: frozenset[Category] = frozenset({"calculation", "comparison"})


@dataclass(frozen=True, slots=True)
class ItemResult:
    question: Question
    answer: Answer
    grade: Grade


@dataclass(frozen=True, slots=True)
class CategoryMetrics:
    n: int
    answered: int
    correctness: float
    citation_support: float


@dataclass(frozen=True, slots=True)
class Metrics:
    n: int
    answerable: int
    unanswerable: int
    correctness: float
    mean_score: float
    citation_support: float
    numeric_accuracy: float
    refusal_recall: float
    false_refusal_rate: float
    hallucination_rate: float
    cost_per_question: float
    latency_p50_ms: float
    latency_p95_ms: float
    by_category: dict[str, CategoryMetrics] = field(default_factory=dict)


def aggregate(results: list[ItemResult]) -> Metrics:
    n = len(results)
    answerable = [r for r in results if not r.question.must_refuse]
    unanswerable = [r for r in results if r.question.must_refuse]
    numeric = [r for r in results if r.question.category in NUMERIC_CATEGORIES]

    scores = [r.grade.correctness for r in answerable if r.grade.correctness is not None]
    correct = sum(1 for s in scores if s >= 1)
    mean_score = (sum(scores) / len(scores) / 2) if scores else 0.0

    citation_numerator = sum(1 for r in answerable if r.grade.citation_support >= 1.0)
    numeric_ok = sum(1 for r in numeric if r.grade.numeric_ok is True)

    latencies = [r.answer.usage.latency_ms for r in results]
    costs = [r.answer.usage.cost_usd for r in results]

    return Metrics(
        n=n,
        answerable=len(answerable),
        unanswerable=len(unanswerable),
        correctness=_ratio(correct, len(answerable)),
        mean_score=mean_score,
        citation_support=_ratio(citation_numerator, len(answerable)),
        numeric_accuracy=_ratio(numeric_ok, len(numeric)),
        refusal_recall=_ratio(sum(1 for r in unanswerable if r.answer.refused), len(unanswerable)),
        false_refusal_rate=_ratio(sum(1 for r in answerable if r.answer.refused), len(answerable)),
        hallucination_rate=_ratio(
            sum(1 for r in unanswerable if not r.answer.refused), len(unanswerable)
        ),
        cost_per_question=(sum(costs) / n) if n else 0.0,
        latency_p50_ms=percentile(latencies, 0.50),
        latency_p95_ms=percentile(latencies, 0.95),
        by_category=_by_category(results),
    )


def _by_category(results: list[ItemResult]) -> dict[str, CategoryMetrics]:
    out: dict[str, CategoryMetrics] = {}
    categories = sorted({r.question.category for r in results})
    for category in categories:
        subset = [r for r in results if r.question.category == category]
        answerable = [r for r in subset if not r.question.must_refuse]
        scores = [r.grade.correctness for r in answerable if r.grade.correctness is not None]
        out[category] = CategoryMetrics(
            n=len(subset),
            answered=sum(1 for r in subset if not r.answer.refused),
            correctness=_ratio(sum(1 for s in scores if s >= 1), len(answerable)),
            citation_support=_ratio(
                sum(1 for r in answerable if r.grade.citation_support >= 1.0),
                len(answerable),
            ),
        )
    return out


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    position = (len(xs) - 1) * p
    low, high = floor(position), ceil(position)
    if low == high:
        return float(xs[low])
    fraction = position - low
    return float(xs[low] * (1 - fraction) + xs[high] * fraction)


def _ratio(numerator: int | float, denominator: int) -> float:
    return float(numerator) / denominator if denominator else 0.0
