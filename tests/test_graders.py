"""Deterministic grading: the cases that would otherwise silently drift."""

from __future__ import annotations

from datetime import date

import pytest

from eval.graders import (
    DeterministicJudge,
    keypoint_coverage,
    numbers_match,
    ratio_slack_for,
    requested_decimals,
)
from eval.schema import Answer, Citation, Gold, Question

TEXT = "Total revenue was 391,035 million dollars, up from 383,285 million."


def question(
    *,
    qid: str = "q1",
    category: str = "fact",
    gold: float | None = 391_035_000_000,
    unit: str = "USD",
    tolerance: float = 0.0001,
    must_refuse: bool = False,
) -> Question:
    return Question(
        id=qid,
        category=category,  # type: ignore[arg-type]
        question="营收是多少？",
        as_of=date(2024, 12, 31),
        universe=["AAPL"],
        gold=Gold(value=gold, unit=unit, tolerance=tolerance) if gold is not None else None,
        must_refuse=must_refuse,
    )


def cited_answer(
    *, text: str = "营收 391,035 百万美元 [K1]", span: tuple[int, int] = (0, 10)
) -> Answer:
    citation = Citation(
        accession="DOC", item_code="8", char_start=span[0], char_end=span[1], quote=TEXT[:10]
    )
    return Answer(text=text, citations=[citation], evidence=[citation])


def test_numbers_match_units_and_percent() -> None:
    assert numbers_match("391,035 百万美元", 3.91035e11, 0.005)[0]
    assert numbers_match("3910.35 亿美元", 3.91035e11, 0.005)[0]
    assert numbers_match("$391.0B", 3.91035e11, 0.00001)[0] is False  # 0.009% off, strict tolerance
    assert numbers_match("$391.035 billion", 3.91035e11, 0.001)[0]
    assert numbers_match("3.91e9", 3.91035e11, 0.005)[0] is False
    assert numbers_match("19.6%", 0.196, 0.005, ratio=True)[0]
    assert numbers_match("0.196", 0.196, 0.005, ratio=True)[0]


def test_ratio_accepts_rounding_to_one_decimal() -> None:
    """4.2% is the correct answer for a question asking for one decimal."""
    assert numbers_match("约 4.2%", 0.041677, 0.005, ratio=True)[0]
    assert numbers_match("18.1%", 0.18145, 0.005, ratio=True)[0]


def test_keypoint_coverage_is_diagnostic_only() -> None:
    assert keypoint_coverage(["391,035 百万美元"], "营收为 391,035 百万美元") > 0.8
    assert keypoint_coverage(["完全无关的内容"], "营收为 391,035 百万美元") < 0.2


async def test_citation_span_must_exist_and_be_in_evidence() -> None:
    judge = DeterministicJudge(section_lookup=lambda acc, item: (0, len(TEXT), TEXT))
    q = question()
    good = await judge.grade(q, cited_answer())
    assert good.correctness == 2
    assert good.citation_support == 1.0

    out_of_bounds = cited_answer(span=(0, len(TEXT) + 5))
    assert (await judge.grade(q, out_of_bounds)).citation_support == 0.0

    # Same span, but the answer never saw it: fabricated.
    citation = Citation(accession="DOC", item_code="8", char_start=0, char_end=10, quote="Total")
    fabricated = Answer(text="营收 391,035 百万美元 [K9]", citations=[citation], evidence=[])
    assert (await judge.grade(q, fabricated)).citation_support == 0.0


async def test_false_refusal_scores_zero() -> None:
    judge = DeterministicJudge(section_lookup=lambda acc, item: (0, len(TEXT), TEXT))
    grade = await judge.grade(question(), Answer(text="无法确认", refused=True))
    assert grade.correctness == 0
    assert grade.notes == "false refusal"


async def test_multi_hop_is_unscored_not_zero() -> None:
    judge = DeterministicJudge(section_lookup=lambda acc, item: (0, len(TEXT), TEXT))
    grade = await judge.grade(
        question(category="multi_hop", gold=None),
        Answer(text="营收增长主要来自服务业务 [K1]", citations=[], evidence=[]),
    )
    assert grade.correctness is None
    assert "unscored" in grade.notes


async def test_unanswerable_answered_is_hallucination() -> None:
    judge = DeterministicJudge(section_lookup=lambda acc, item: (0, len(TEXT), TEXT))
    grade = await judge.grade(question(gold=None, must_refuse=True), Answer(text="999"))
    assert grade.hallucination is True
    assert grade.correctness == 0


@pytest.mark.parametrize(
    ("text", "gold", "tolerance", "ratio", "expected"),
    [
        ("391,035 百万美元", 3.91035e11, 0.005, False, True),
        ("3910.35 亿美元", 3.91035e11, 0.005, False, True),
        ("$391.035 billion", 3.91035e11, 0.001, False, True),
        ("$391.0B", 3.91035e11, 0.00001, False, False),
        ("3.91e9", 3.91035e11, 0.005, False, False),
        ("19.6%", 0.196, 0.005, True, True),
        ("0.196", 0.196, 0.005, True, True),
        ("4.2%", 0.041677, 0.005, True, True),
        ("18.1%", 0.18145, 0.005, True, True),
        ("1.41 倍", 1.410108, 0.005, True, True),
        ("2.53 倍", 2.54713, 0.005, True, False),
        ("648,125 百万", 6.48125e11, 0.001, False, True),
        ("87,450 千美元", 8.745e7, 0.005, False, True),
        ("99.92 亿美元", 9.992e9, 0.005, False, True),
        ("391035000000 美元", 3.91035e11, 0.0001, False, True),
    ],
)
def test_number_matrix(
    text: str, gold: float, tolerance: float, ratio: bool, expected: bool
) -> None:
    assert numbers_match(text, gold, tolerance, ratio=ratio)[0] is expected


def test_requested_decimals_reads_the_question_text() -> None:
    assert requested_decimals("保留一位小数（百分比）") == 1
    assert requested_decimals("保留两位小数。") == 2
    assert requested_decimals("请给出倍数") is None


def test_ratio_slack_follows_requested_precision() -> None:
    """``保留两位小数`` on a 0.25x ratio means 0.25 is right, not 1.4% wrong."""
    two_dp = Question(
        id="h-c2-004",
        category="comparison",
        question="苹果 FY2024 的资本开支是 Meta 的多少倍？保留两位小数。",
        as_of=date(2025, 6, 30),
        universe=["AAPL", "META"],
        gold=Gold(value=0.25357, unit="ratio"),
    )
    slack = ratio_slack_for(two_dp)
    assert slack == pytest.approx(0.005)
    assert numbers_match("约 0.25 倍", 0.25357, 0.005, ratio=True, ratio_slack=slack)[0]
    # A wrong ratio (2.53 vs 2.547, 0.66% off) must still fail: the slack is 0.005.
    assert not numbers_match("约 2.53 倍", 2.54713, 0.005, ratio=True, ratio_slack=slack)[0]

    percent = Question(
        id="c1-006",
        category="calculation",
        question="营业利润率是多少？保留一位小数（百分比）。",
        as_of=date(2025, 6, 30),
        universe=["COST"],
        gold=Gold(value=0.036491, unit="percent"),
    )
    assert ratio_slack_for(percent) == pytest.approx(0.001)
    assert numbers_match(
        "约 3.6%", 0.036491, 0.005, ratio=True, ratio_slack=ratio_slack_for(percent)
    )[0]
