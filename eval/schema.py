"""Contracts shared by every agent and every metric.

``Answer`` is the seam that keeps evaluation independent of implementation: the
hand-written ReAct loop (W2) and the LangGraph state machine (W4) both satisfy
it, so ``eval/`` never changes when the agent does.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

Category = Literal[
    "fact",  # F1 单点事实
    "multi_hop",  # F2 多跳/跨文档
    "calculation",  # C1 数值计算
    "comparison",  # C2 跨公司对比
    "unanswerable",  # R1 不可答
    "adversarial",  # A1 对抗
]

CATEGORY_LABELS: dict[Category, str] = {
    "fact": "F1 事实",
    "multi_hop": "F2 多跳",
    "calculation": "C1 计算",
    "comparison": "C2 对比",
    "unanswerable": "R1 不可答",
    "adversarial": "A1 对抗",
}


class Citation(BaseModel):
    accession: str
    item_code: str
    char_start: int
    char_end: int
    quote: str = ""


class Usage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0  # 命中缓存的那部分输入
    model: str = ""  # served model，不是请求时用的别名
    cost_usd: float = 0.0
    peak: bool = False  # 请求落在高峰时段时记 True（成本归因用）
    latency_ms: int = 0
    tool_calls: int = 0


class Answer(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)
    # Every span the agent actually saw. A citation is only honest if it is also
    # in here; the judge checks that, so a fabricated reference cannot pass.
    evidence: list[Citation] = Field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    trace_id: str = ""


class Gold(BaseModel):
    value: float | None = None
    unit: str | None = None
    tolerance: float = 0.005


class GoldSource(BaseModel):
    kind: Literal["xbrl", "ratio", "text", "none"] = "none"
    cik: str | None = None
    tag: str | None = None
    period_end: date | None = None
    # ratio only: gold = value(cik, tag) / value(denominator_cik, denominator_tag)
    denominator_cik: str | None = None
    denominator_tag: str | None = None
    denominator_period_end: date | None = None
    note: str | None = None


class Question(BaseModel):
    id: str
    category: Category
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    question: str
    as_of: date
    universe: list[str]
    period_spec: dict[str, str] = Field(default_factory=dict)
    gold: Gold | None = None
    gold_source: GoldSource = Field(default_factory=GoldSource)
    required_citations: list[str] = Field(default_factory=list)
    key_points: list[str] = Field(default_factory=list)
    must_refuse: bool = False
    corpus: Literal["edgar", "adversarial"] = "edgar"
    corpus_ref: str | None = None
    rubric: dict[str, str] = Field(default_factory=dict)
    author_note: str = ""


class Grade(BaseModel):
    """One judge's verdict on one answer."""

    correctness: int | None = None  # 0/1/2; None = not scored (refusal questions)
    citation_support: float = 0.0  # 0..1, per-question
    numeric_ok: bool | None = None
    hallucination: bool = False
    notes: str = ""


@runtime_checkable
class Agent(Protocol):
    name: str

    async def answer(self, question: Question) -> Answer: ...


@runtime_checkable
class Judge(Protocol):
    name: str

    async def grade(self, question: Question, answer: Answer) -> Grade: ...
