"""W4 graph tests: routing, verify/repair, and interrupt/resume.

The LLM and the tool backend are scripted so these tests assert *graph*
behaviour, not model quality. The Postgres test proves the checkpoint survives a
new agent instance (the in-process half of "resume after kill -9").
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from agent.graph import GraphAgent, GraphConfig
from agent.graph.agent import BackendResult
from agent.llm import EmptyResponseError, LLMResult, Message
from eval.schema import Question
from pipelines.config import Settings


def _result(text: str) -> LLMResult:
    return LLMResult(
        text=text,
        model="fake",
        tokens_in=10,
        tokens_cached=0,
        tokens_out=5,
        cost_usd=0.0001,
        peak=False,
        latency_ms=1,
        finish_reason="stop",
        created_at="2026-11-11T00:00:00Z",
    )


class ScriptedLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 0,
        json_mode: bool = False,
        temperature: float = 0.0,
    ) -> LLMResult:
        self.calls += 1
        if not self.replies:
            raise EmptyResponseError("script exhausted")
        return _result(self.replies.pop(0))


class StubBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, store, name: str, args: dict) -> BackendResult:  # type: ignore[no-untyped-def]
        self.calls.append(name)
        if name == "search_filings":
            handle = store.add(
                accession="AAPL-10K-FY2024",
                item_code="7",
                char_start=0,
                char_end=20,
                quote="revenue was 391 billion",
                label="AAPL 10-K",
                filed_at=date(2024, 11, 1),
            )
            return BackendResult(True, None, json.dumps({"results": [{"handle": handle.name}]}))
        return BackendResult(False, "unknown_tool", "no such tool")


def _question() -> Question:
    return Question(
        id="g-001",
        category="fact",
        question="苹果 FY2024 营收是多少？",
        as_of=date(2024, 12, 31),
        universe=["AAPL"],
    )


def _tool_reply(query: str = "revenue") -> str:
    return json.dumps(
        {
            "thought": "先检索",
            "action": "search_filings",
            "action_input": {"query": query, "tickers": ["AAPL"], "as_of": "2024-12-31"},
        }
    )


def _final(text: str, citations: list[str]) -> str:
    return json.dumps(
        {
            "thought": "给出答案",
            "action": "final_answer",
            "action_input": {"text": text, "citations": citations},
        }
    )


def _agent(
    settings: Settings, llm: ScriptedLLM, *, config: GraphConfig, tmp_path: Path
) -> GraphAgent:
    return GraphAgent(
        replace(settings, data_dir=tmp_path),
        llm=llm,
        config=config,
        backend=StubBackend(),
        checkpointer="memory",
    )


@pytest.mark.asyncio
async def test_graph_answers_with_citation(tmp_path: Path) -> None:
    settings = Settings.from_env()
    llm = ScriptedLLM([_tool_reply(), _final("营收 3910 亿美元 [K1]", ["K1"])])
    agent = _agent(settings, llm, config=GraphConfig(verify_semantic=False), tmp_path=tmp_path)
    answer = await agent.answer(_question())

    assert answer.refused is False
    assert [c.handle for c in answer.citations] == ["K1"]
    assert answer.usage.tool_calls == 1
    assert answer.trace_id
    assert list((tmp_path / "traces").glob("*.json"))


@pytest.mark.asyncio
async def test_verify_rejects_unknown_handle_and_repairs(tmp_path: Path) -> None:
    settings = Settings.from_env()
    llm = ScriptedLLM(
        [
            _tool_reply(),
            _final("营收 3910 亿美元 [K9]", ["K9"]),  # K9 never existed
            _final("营收 3910 亿美元 [K1]", ["K1"]),  # repair uses a real handle
        ]
    )
    agent = _agent(settings, llm, config=GraphConfig(verify_semantic=False), tmp_path=tmp_path)
    answer = await agent.answer(_question())

    assert answer.refused is False
    assert answer.verification_failed is False
    assert [c.handle for c in answer.citations] == ["K1"]
    assert llm.calls == 3  # tool turn + bad draft + repaired draft


@pytest.mark.asyncio
async def test_unrepairable_answer_is_marked_not_dropped(tmp_path: Path) -> None:
    settings = Settings.from_env()
    llm = ScriptedLLM(
        [
            _tool_reply(),
            _final("营收 3910 亿美元 [K9]", ["K9"]),
            _final("营收 3910 亿美元 [K8]", ["K8"]),
            _final("营收 3910 亿美元 [K7]", ["K7"]),
        ]
    )
    agent = _agent(
        settings,
        llm,
        config=GraphConfig(verify_semantic=False, max_repairs=1, review_mode="never"),
        tmp_path=tmp_path,
    )
    answer = await agent.answer(_question())

    assert answer.verification_failed is True
    assert answer.refused is False  # eval must get an answer; the flag marks it


@pytest.mark.asyncio
async def test_review_interrupt_then_resume(tmp_path: Path) -> None:
    settings = Settings.from_env()
    llm = ScriptedLLM([_tool_reply(), _final("营收 3910 亿美元 [K1]", ["K1"])])
    agent = _agent(
        settings,
        llm,
        config=GraphConfig(verify_semantic=False, review_mode="always"),
        tmp_path=tmp_path,
    )

    started = await agent.start_review(_question())
    assert started["status"] == "awaiting_review"
    assert started["payload"]["draft"]["text"]

    answer = await agent.resume(started["thread_id"], {"action": "approve"})
    assert answer.text.startswith("营收")
    assert answer.trace_id == started["thread_id"]


@pytest.mark.asyncio
async def test_review_reject_turns_into_refusal(tmp_path: Path) -> None:
    settings = Settings.from_env()
    llm = ScriptedLLM([_tool_reply(), _final("营收 3910 亿美元 [K1]", ["K1"])])
    agent = _agent(
        settings,
        llm,
        config=GraphConfig(verify_semantic=False, review_mode="always"),
        tmp_path=tmp_path,
    )
    started = await agent.start_review(_question())
    answer = await agent.resume(started["thread_id"], {"action": "reject"})

    assert answer.refused is True
    assert "人工复核" in answer.text


@pytest.mark.asyncio
async def test_postgres_checkpoint_survives_new_agent(tmp_path: Path) -> None:
    """A second agent instance (new connection) must be able to resume."""
    settings = Settings.from_env()
    config = GraphConfig(verify_semantic=False, review_mode="always")

    first = GraphAgent(
        replace(settings, data_dir=tmp_path),
        llm=ScriptedLLM([_tool_reply(), _final("营收 3910 亿美元 [K1]", ["K1"])]),
        config=config,
        backend=StubBackend(),
        checkpointer="postgres",
    )
    started = await first.start_review(_question())
    assert started["status"] == "awaiting_review"
    await first.aclose()

    second = GraphAgent(
        replace(settings, data_dir=tmp_path),
        llm=ScriptedLLM([]),  # resume must not need another LLM call
        config=config,
        backend=StubBackend(),
        checkpointer="postgres",
    )
    answer = await second.resume(started["thread_id"], {"action": "approve"})
    await second.aclose()

    assert answer.text.startswith("营收")
    assert answer.trace_id == started["thread_id"]


@pytest.mark.asyncio
async def test_refusal_is_not_sent_into_repair(tmp_path: Path) -> None:
    """A legitimate refusal has no claims to verify; repairing it wastes two rounds."""
    settings = Settings.from_env()
    llm = ScriptedLLM([_final("无法确认：财报正文与 XBRL 都没有披露该口径。", [])])
    agent = _agent(settings, llm, config=GraphConfig(verify_semantic=True), tmp_path=tmp_path)
    answer = await agent.answer(_question())

    assert answer.refused is True
    assert llm.calls == 1
    assert answer.verification_failed is False


@pytest.mark.asyncio
async def test_step_limit_gets_a_finalize_salvage_call(tmp_path: Path) -> None:
    """Parity with the hand-written loop: no-tools finalize instead of a bare refusal."""
    settings = Settings.from_env()
    llm = ScriptedLLM(
        [
            _tool_reply("revenue"),
            _tool_reply("net income"),
            _final("营业收入 3910 亿美元 [K1]", ["K1"]),
        ]
    )
    agent = _agent(
        settings,
        llm,
        config=GraphConfig(verify_semantic=False, max_steps=2),
        tmp_path=tmp_path,
    )
    answer = await agent.answer(_question())

    assert llm.calls == 3
    assert answer.refused is False
    assert "3910" in answer.text
