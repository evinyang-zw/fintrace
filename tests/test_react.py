"""The hand-written loop: parsing, termination and citation mapping."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from agent.llm import LLMResult, Message
from agent.react import ReactAgent, ReactConfig, parse_action
from agent.tools import EvidenceStore, ToolOutcome
from eval.schema import Question
from pipelines.config import Settings


def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url="postgresql://unused",
        sec_user_agent="test",
        llm_model="fake-model",
    )


def question() -> Question:
    return Question(
        id="q-test",
        category="fact",
        question="营收是多少？",
        as_of=date(2024, 12, 31),
        universe=["AAPL"],
    )


class ScriptedLLM:
    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[list[Message]] = []

    async def complete(self, messages, *, max_tokens=1200, json_mode=True, temperature=None):
        self.prompts.append(list(messages))
        text = (
            self.replies.pop(0)
            if self.replies
            else '{"action": "final_answer", "action_input": {"text": "无"}}'
        )
        return LLMResult(
            text=text,
            model="fake-model",
            tokens_in=100,
            tokens_cached=0,
            tokens_out=20,
            cost_usd=0.0001,
            peak=False,
            latency_ms=1,
            finish_reason="stop",
            created_at="2026-10-29T00:00:00+00:00",
        )


class FakeTools:
    def __init__(self, store: EvidenceStore) -> None:
        self.store = store
        self.calls = 0

    def execute(self, name: str, args: dict) -> ToolOutcome:
        self.calls += 1
        handle = self.store.add(
            accession="DOC",
            item_code="8",
            char_start=0,
            char_end=25,
            quote="Total revenue was 391,035",
            label="AAPL/10-K/2024-09-28",
            filed_at=date(2024, 11, 1),
        )
        return ToolOutcome(ok=True, data={"handle": handle.name, "snippet": "revenue 391,035"})


def test_parse_action_tolerates_prose_and_fences() -> None:
    fenced = (
        '```json\n{"thought": "t", "action": "compute", "action_input": {"expression": "1+1"}}\n```'
    )
    assert parse_action(fenced) == ("compute", {"expression": "1+1"}, "t")
    assert parse_action("not json") is None
    assert parse_action('{"action": "x"}') == ("x", {}, "")


async def test_loop_maps_handles_to_citations(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            '{"thought": "先查", "action": "search_filings", "action_input": {"query": "revenue"}}',
            '{"thought": "够了", "action": "final_answer", '
            '"action_input": {"text": "营收 391,035 百万美元 [K1]", "citations": ["K1"]}}',
        ]
    )
    agent = ReactAgent(
        settings(tmp_path),
        llm=llm,  # type: ignore[arg-type]
        config=ReactConfig(max_steps=4, timeout_s=10),
        tools_factory=lambda store: FakeTools(store),  # type: ignore[arg-type]
    )
    answer = await agent.answer(question())
    assert not answer.refused
    assert len(answer.citations) == 1
    assert answer.citations[0].accession == "DOC"
    assert len(answer.evidence) == 1
    assert answer.usage.tool_calls == 1
    trace_path = tmp_path / "traces" / f"{answer.trace_id}.json"
    assert trace_path.is_file()
    payload = json.loads(trace_path.read_text())
    assert [step["action"] for step in payload["steps"]] == ["search_filings", "final_answer"]


async def test_unknown_handle_is_dropped(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            '{"thought": "编一个", "action": "final_answer", '
            '"action_input": {"text": "营收 1 亿 [K7]", "citations": ["K7"]}}'
        ]
    )
    agent = ReactAgent(
        settings(tmp_path),
        llm=llm,  # type: ignore[arg-type]
        config=ReactConfig(max_steps=2, timeout_s=10),
        tools_factory=lambda store: FakeTools(store),  # type: ignore[arg-type]
    )
    answer = await agent.answer(question())
    assert answer.citations == []


async def test_unparseable_reply_gets_one_repair(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            "I will now search the filings.",  # not JSON
            '{"thought": "ok", "action": "final_answer", "action_input": {"text": "无法确认：证据不足"}}',
        ]
    )
    agent = ReactAgent(
        settings(tmp_path),
        llm=llm,  # type: ignore[arg-type]
        config=ReactConfig(max_steps=3, timeout_s=10),
        tools_factory=lambda store: FakeTools(store),  # type: ignore[arg-type]
    )
    answer = await agent.answer(question())
    assert answer.refused
    assert any("不是合法的 action JSON" in m.content for m in llm.prompts[1])


async def test_step_limit_forces_a_final_answer(tmp_path: Path) -> None:
    tool_call = '{"thought": "再查", "action": "search_filings", "action_input": {"query": "x"}}'
    llm = ScriptedLLM([tool_call, tool_call, tool_call, tool_call])
    agent = ReactAgent(
        settings(tmp_path),
        llm=llm,  # type: ignore[arg-type]
        config=ReactConfig(max_steps=2, timeout_s=10),
        tools_factory=lambda store: FakeTools(store),  # type: ignore[arg-type]
    )
    answer = await agent.answer(question())
    assert answer.refused
    assert answer.refusal_reason in {"step_limit", "duplicate_calls"}
