"""Rerank parser and LLM judge behaviour, with fakes instead of network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.rerank import DashScopeReranker, RerankError
from eval.citation import CitationChecker, extract_claims
from eval.llm_judge import LLMJudge
from eval.schema import Answer, Citation, Question
from pipelines.config import Settings


def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url="postgresql://unused",
        sec_user_agent="test",
        llm_model="fake-model",
    )


def question(*, must_refuse: bool = False) -> Question:
    return Question(
        id="q1",
        category="fact",
        question="营收是多少？",
        as_of=date(2025, 1, 1),
        universe=["TST"],
        key_points=["3910 亿美元"],
        must_refuse=must_refuse,
    )


class FakeLLM:
    name = "fake"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, messages: list[Any], **kwargs: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(text=self.replies.pop(0))


# -- rerank ---------------------------------------------------------------


def test_rerank_parse_sorts_and_bounds() -> None:
    reranker = DashScopeReranker(url="http://unused", api_key="k", model="gte-rerank")
    payload = {
        "output": {
            "results": [
                {"index": 1, "relevance_score": 0.2},
                {"index": 0, "relevance_score": 0.9},
                {"index": 9, "relevance_score": 0.99},
            ]
        }
    }
    assert reranker._parse(payload, limit=2) == [(0, 0.9), (1, 0.2)]


def test_rerank_parse_rejects_empty_output() -> None:
    reranker = DashScopeReranker(url="http://unused", api_key="k", model="gte-rerank")
    with pytest.raises(RerankError):
        reranker._parse({"output": {"results": []}}, limit=3)


# -- llm judge ------------------------------------------------------------


async def test_judge_parses_and_caches(tmp_path: Path) -> None:
    llm = FakeLLM(['{"correctness": 2, "reason": "数字与要点一致"}'])
    judge = LLMJudge(settings(tmp_path), llm=llm, cache_dir=tmp_path / "cache")
    answer = Answer(text="3910 亿美元 [K1]")

    first = await judge.grade(question(), answer)
    second = await judge.grade(question(), answer)

    assert first.correctness == 2
    assert second.correctness == 2
    assert llm.calls == 1  # second verdict came from the cache


async def test_judge_retries_once_on_bad_json(tmp_path: Path) -> None:
    llm = FakeLLM(["not json", '{"correctness": 1, "reason": "要点不全"}'])
    judge = LLMJudge(settings(tmp_path), llm=llm, cache_dir=tmp_path / "cache")
    grade = await judge.grade(question(), Answer(text="大约 3900 亿美元"))
    assert grade.correctness == 1
    assert llm.calls == 2


async def test_judge_gives_up_transparently(tmp_path: Path) -> None:
    llm = FakeLLM(["nope", "still nope"])
    judge = LLMJudge(settings(tmp_path), llm=llm, cache_dir=tmp_path / "cache")
    grade = await judge.grade(question(), Answer(text="不知道"))
    assert grade.correctness is None
    assert grade.notes.startswith("judge_error")


async def test_judge_rules_refusal_without_calling_llm(tmp_path: Path) -> None:
    llm = FakeLLM([])
    judge = LLMJudge(settings(tmp_path), llm=llm)
    grade = await judge.grade(question(must_refuse=True), Answer(text="无法确认", refused=True))
    assert grade.correctness is None
    assert llm.calls == 0


# -- citation -------------------------------------------------------------


def test_extract_claims_counts_uncited_sentences() -> None:
    claims, uncited = extract_claims("营收 3910 亿 [K1]。利润率上升 [K2]。这是一句没有引用的话。")
    assert [claim.handles for claim in claims] == [("K1",), ("K2",)]
    assert uncited == 1


def test_extract_claims_keeps_decimal_points_intact() -> None:
    claims, _ = extract_claims("毛利率为 72.7% [K1]。")
    assert len(claims) == 1


async def test_citation_checker_scores_partial_half(tmp_path: Path) -> None:
    payload = json.dumps(
        {"results": [{"i": 1, "label": "supported"}, {"i": 2, "label": "partial"}]},
        ensure_ascii=False,
    )
    checker = CitationChecker(
        settings(tmp_path),
        llm=FakeLLM([payload]),
        section_lookup=lambda accession, item: (0, "Revenue 391.0 billion; margin up " * 20),
    )
    answer = Answer(
        text="营收 3910 亿 [K1]。毛利率上升 [K2]。",
        citations=[
            Citation(accession="a", item_code="7", char_start=0, char_end=10, handle="K1"),
            Citation(accession="a", item_code="7", char_start=10, char_end=20, handle="K2"),
        ],
    )
    report = await checker.check(question(), answer)
    assert report.supported == 1
    assert report.partial == 1
    assert report.score == pytest.approx(0.75)


async def test_citation_checker_flags_uncited_answer(tmp_path: Path) -> None:
    checker = CitationChecker(settings(tmp_path), llm=FakeLLM([]))
    report = await checker.check(question(), Answer(text="营收 3910 亿，没有任何引用。"))
    assert report.score == 0.0
    assert report.total == 0


async def test_w3_judge_scores_multi_hop_refusal_as_zero(tmp_path: Path) -> None:
    from eval.judge import W3Judge

    class StubDet:
        name = "stub"

        async def grade(self, question: Any, answer: Any) -> Any:
            from eval.schema import Grade

            return Grade(correctness=None, notes="det would leave multi-hop unscored")

    class Explode:
        async def grade(self, question: Any, answer: Any) -> Any:  # pragma: no cover
            raise AssertionError("llm judge must not run on a refusal")

        async def check(self, question: Any, answer: Any) -> Any:  # pragma: no cover
            raise AssertionError("citation checker must not run on a refusal")

    multi_hop = Question(
        id="f2-x",
        category="multi_hop",
        question="为什么毛利率变化？",
        as_of=date(2025, 1, 1),
        universe=["TST"],
        key_points=["a", "b", "c"],
    )
    judge = W3Judge(
        settings(tmp_path),
        llm_judge=Explode(),  # type: ignore[arg-type]
        deterministic=StubDet(),  # type: ignore[arg-type]
        citation_checker=Explode(),  # type: ignore[arg-type]
    )
    grade = await judge.grade(multi_hop, Answer(text="无法确认", refused=True))
    assert grade.correctness == 0
