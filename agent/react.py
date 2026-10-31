"""A hand-written ReAct loop, deliberately without a framework.

The point is not to avoid LangGraph forever; it is to know exactly what the
framework will later do for us (state, retries, interrupts, checkpoints). The
comparison becomes ADR-002 and, later, the reason we adopt a graph at all.

Termination is the hard part, so every escape hatch is explicit: step limit,
budget, duplicate calls, unparseable output, and a forced final answer.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.llm import EmptyResponseError, LLMClient, LLMError, LLMResult, Message, build_llm
from agent.prompts import (
    PROMPT_VERSION,
    build_finalize_prompt,
    build_system_prompt,
    build_task_prompt,
)
from agent.retrieval import Retriever
from agent.tools import TOOL_SPECS, EvidenceStore, ToolExecutor, citation_from, evidence_citations
from agent.trace import Trace, TraceStep
from eval.schema import Answer, Citation, FactCitation, Question, Usage
from pipelines.config import Settings

REPAIR_PROMPT = (
    "上一条不是合法的 action JSON。只输出一个 JSON 对象，"
    '格式：{"thought": "...", "action": "工具名或 final_answer", "action_input": {...}}。'
    "不要 Markdown，不要额外文字。"
)
REFUSAL_PREFIX = "无法确认"
CONTEXT_TOKEN_LIMIT = 8000  # observations beyond this get replaced, not dropped


@dataclass(frozen=True, slots=True)
class ReactConfig:
    max_steps: int = 7
    max_tokens_per_step: int = 1200
    budget_usd: float = 0.25
    timeout_s: float = 90.0


def parse_action(text: str) -> tuple[str, dict[str, Any], str] | None:
    """Extract one action object from a model reply, tolerating stray prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    action = payload.get("action")
    raw_args = payload.get("action_input", payload.get("actionInput", {}))
    thought = payload.get("thought", "")
    if not isinstance(action, str) or not isinstance(raw_args, dict):
        return None
    return action, raw_args, str(thought)


def _truncate(text: str, limit: int = 2600) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…[{len(text) - limit} chars elided]"


class _Run:
    """All mutable state for one question; nothing leaks between concurrent runs."""

    def __init__(
        self,
        question: Question,
        llm: LLMClient,
        settings: Settings,
        config: ReactConfig,
        tools_factory: Callable[[EvidenceStore], ToolExecutor],
    ) -> None:
        self.question = question
        self.llm = llm
        self.settings = settings
        self.config = config
        self.store = EvidenceStore()
        self.tools = tools_factory(self.store)
        self.trace_id = f"{datetime.now():%Y%m%d-%H%M%S}-{question.id}-{uuid4().hex[:6]}"
        self.trace = Trace(
            trace_id=self.trace_id,
            question=question,
            meta={
                "agent": "react",
                "prompt_version": PROMPT_VERSION,
                "model": settings.llm_model,
                "temperature": settings.llm_temperature,
                "thinking": settings.llm_thinking,
            },
        )
        self.messages: list[Message] = [
            Message("system", build_system_prompt(TOOL_SPECS, config.max_steps)),
            Message("user", build_task_prompt(question, config.max_steps)),
        ]
        self.cost_usd = 0.0
        self.tokens_in = 0
        self.tokens_cached = 0
        self.tokens_out = 0
        self.latency_ms = 0
        self.served_model = ""
        self.peak = False
        self.tool_calls = 0
        self._signatures: dict[str, int] = {}

    # -- LLM bookkeeping --------------------------------------------------

    async def ask(self, messages: Sequence[Message]) -> LLMResult:
        result = await self.llm.complete(
            messages,
            max_tokens=self.config.max_tokens_per_step,
            json_mode=True,
            temperature=self.settings.llm_temperature,
        )
        self.cost_usd += result.cost_usd
        self.tokens_in += result.tokens_in
        self.tokens_cached += result.tokens_cached
        self.tokens_out += result.tokens_out
        self.latency_ms += result.latency_ms
        self.served_model = result.model
        self.peak = self.peak or result.peak
        return result

    def usage(self) -> Usage:
        return Usage(
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            tokens_cached=self.tokens_cached,
            model=self.served_model or self.settings.llm_model,
            cost_usd=round(self.cost_usd, 8),
            peak=self.peak,
            latency_ms=self.latency_ms,
            tool_calls=self.tool_calls,
        )

    # -- loop -------------------------------------------------------------

    async def execute(self) -> Answer:
        try:
            for index in range(1, self.config.max_steps + 1):
                if self.cost_usd >= self.config.budget_usd:
                    self.trace.notes.append(f"step {index}: budget exhausted before call")
                    return await self.finalize("budget_exhausted", allow_call=False)

                try:
                    result = await self.ask(self.messages)
                except EmptyResponseError as exc:
                    # One retry: an empty body is usually a max_tokens accident,
                    # not a contract violation.
                    self.trace.notes.append(f"step {index}: empty response, retry once: {exc}")
                    try:
                        result = await self.ask(self.messages)
                    except LLMError as retry_exc:
                        self.trace.notes.append(f"step {index}: empty again: {retry_exc}")
                        return self._refusal("empty_response")
                except LLMError as exc:
                    self.trace.notes.append(f"step {index}: llm error: {exc}")
                    return self._refusal("llm_error")

                self.messages.append(Message("assistant", result.text))
                parsed = parse_action(result.text)
                if parsed is None:
                    self.trace.notes.append(f"step {index}: unparseable reply")
                    self.trace.steps.append(
                        TraceStep(
                            index=index,
                            thought="",
                            action="(unparseable)",
                            args={},
                            ok=False,
                            code="parse_error",
                            observation=_truncate(result.text, 500),
                            tool_latency_ms=0,
                            tokens_in=result.tokens_in,
                            tokens_cached=result.tokens_cached,
                            tokens_out=result.tokens_out,
                            cost_usd=result.cost_usd,
                            llm_latency_ms=result.latency_ms,
                            peak=result.peak,
                        )
                    )
                    self.messages.append(Message("user", REPAIR_PROMPT))
                    self._trim_context()
                    continue

                action, args, thought = parsed
                if action == "final_answer":
                    return self.build_answer(args, thought=thought, index=index)
                if action == "fail":
                    return self._refusal("agent_failed")

                signature = action + json.dumps(args, sort_keys=True, ensure_ascii=False)
                seen = self._signatures.get(signature, 0)
                self._signatures[signature] = seen + 1
                if seen >= 1:
                    note = (
                        f"你已经用相同参数调用过 {action}，结果不会变。"
                        "换关键词/换工具，或直接给 final_answer。"
                    )
                    self.messages.append(Message("user", note))
                    if seen >= 2:
                        self.trace.notes.append(f"step {index}: duplicate x3, forcing answer")
                        return await self.finalize("duplicate_calls")
                    continue

                started = time.perf_counter()
                # Tools do blocking DB/HTTP work (embedding, rerank). Running them
                # on the event loop serialises every concurrent question and
                # inflates wall-clock p95; a thread keeps the loop free.
                outcome = await asyncio.to_thread(self.tools.execute, action, args)
                elapsed = int((time.perf_counter() - started) * 1000)
                self.tool_calls += 1
                observation = outcome.to_observation()
                self.trace.steps.append(
                    TraceStep(
                        index=index,
                        thought=thought,
                        action=action,
                        args=args,
                        ok=outcome.ok,
                        code=outcome.code,
                        observation=_truncate(observation),
                        tool_latency_ms=elapsed,
                        tokens_in=result.tokens_in,
                        tokens_cached=result.tokens_cached,
                        tokens_out=result.tokens_out,
                        cost_usd=result.cost_usd,
                        llm_latency_ms=result.latency_ms,
                        peak=result.peak,
                    )
                )
                self.messages.append(
                    Message(
                        "user",
                        f"TOOL RESULT {action}:\n{_truncate(observation)}\n"
                        f"当前可用句柄: {self.store.known()}",
                    )
                )
                self._trim_context()

            self.trace.notes.append(f"step limit {self.config.max_steps} reached")
            return await self.finalize("step_limit")
        finally:
            self.write_trace()

    def _trim_context(self) -> None:
        """Drop the *content* of old observations, never the message structure.

        The system prompt and the original question stay; the newest observation
        stays; older ones become a one-line placeholder so handles cited earlier
        remain conceptually valid.
        """

        def total() -> int:
            return sum(len(message.content) // 4 for message in self.messages)

        if total() <= CONTEXT_TOKEN_LIMIT:
            return
        for position, message in enumerate(self.messages):
            if position < 2 or message.role != "user":
                continue
            if not message.content.startswith("TOOL RESULT"):
                continue
            if message.content.startswith("TOOL RESULT (trimmed"):
                continue
            self.messages[position] = Message(
                "user",
                "TOOL RESULT (trimmed): 较早的观察已裁剪以控制上下文；此前给出的句柄仍然有效。",
            )
            if total() <= CONTEXT_TOKEN_LIMIT:
                return

    def write_trace(self) -> None:
        try:
            self.trace.write(Path(self.settings.data_dir) / "traces")
        except Exception as exc:  # tracing must never break evaluation
            print(f"warning: could not write trace {self.trace_id}: {exc}")

    # -- endings ----------------------------------------------------------

    def _refusal(self, reason: str, text: str | None = None) -> Answer:
        answer = Answer(
            text=text or f"{REFUSAL_PREFIX}：本次运行未能取得足够证据（{reason}）。",
            citations=[],
            evidence=evidence_citations(self.store),
            retrieval=list(self.store.search_calls),
            refused=True,
            refusal_reason=reason,
            usage=self.usage(),
            trace_id=self.trace_id,
        )
        self.trace.answer = answer
        return answer

    async def finalize(self, reason: str, *, allow_call: bool = True) -> Answer:
        """One last LLM call, with no tools allowed, to salvage partial evidence."""
        if allow_call and self.cost_usd < self.config.budget_usd * 1.2:
            try:
                result = await self.ask(
                    [*self.messages, Message("user", build_finalize_prompt(reason))]
                )
                self.trace.steps.append(
                    TraceStep(
                        index=len(self.trace.steps) + 1,
                        thought="",
                        action="finalize_prompt",
                        args={"reason": reason},
                        ok=True,
                        code=None,
                        observation=_truncate(result.text, 400),
                        tool_latency_ms=0,
                        tokens_in=result.tokens_in,
                        tokens_cached=result.tokens_cached,
                        tokens_out=result.tokens_out,
                        cost_usd=result.cost_usd,
                        llm_latency_ms=result.latency_ms,
                        peak=result.peak,
                    )
                )
                parsed = parse_action(result.text)
                if parsed is not None and parsed[0] == "final_answer":
                    return self.build_answer(
                        parsed[1], thought=parsed[2], index=len(self.trace.steps) + 1
                    )
                self.trace.notes.append(f"finalize({reason}): reply was not final_answer")
            except LLMError as exc:
                self.trace.notes.append(f"finalize({reason}): {exc}")
        return self._refusal(reason)

    def build_answer(self, payload: dict[str, Any], *, thought: str, index: int) -> Answer:
        raw_text = payload.get("text")
        text = str(raw_text).strip() if raw_text is not None else ""
        names: list[str] = []
        raw_citations = payload.get("citations")
        if isinstance(raw_citations, list):
            names.extend(str(item) for item in raw_citations)
        names.extend(re.findall(r"\[(K\d+)\]", text))
        fact_names = list(re.findall(r"\[(F\d+)\]", text))
        fact_names.extend(name for name in names if name.startswith("F"))
        names = [name for name in names if name.startswith("K")]

        facts = [
            FactCitation(
                handle=fact.name,
                ticker=fact.ticker,
                tag=fact.tag,
                value=fact.value,
                unit=fact.unit,
                period_end=fact.period_end,
                fiscal_period=fact.fiscal_period,
            )
            for name in dict.fromkeys(fact_names)
            if (fact := self.store.facts.get(name)) is not None
        ]

        citations: list[Citation] = []
        seen: set[tuple[str, str, int, int]] = set()
        for name in names:
            handle = self.store.get(name)
            if handle is None:
                self.trace.notes.append(f"unknown handle cited: {name}")
                continue
            key = (handle.accession, handle.item_code, handle.char_start, handle.char_end)
            if key in seen:
                continue
            seen.add(key)
            citations.append(citation_from(handle))

        refused = payload.get("refused") is True or not text or text.startswith(REFUSAL_PREFIX)
        answer = Answer(
            text=text or f"{REFUSAL_PREFIX}：没有可用的最终答案。",
            citations=citations,
            facts=facts,
            evidence=evidence_citations(self.store),
            retrieval=list(self.store.search_calls),
            refused=refused,
            refusal_reason="insufficient_evidence" if refused else None,
            usage=self.usage(),
            trace_id=self.trace_id,
        )
        if not citations and not facts and not refused:
            self.trace.notes.append("answered without any citation")
        self.trace.answer = answer
        self.trace.steps.append(
            TraceStep(
                index=index,
                thought=thought,
                action="final_answer",
                args={"citations": names},
                ok=True,
                code=None,
                observation=_truncate(text, 500),
                tool_latency_ms=0,
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
            )
        )
        return answer


class ReactAgent:
    """Satisfies ``eval.schema.Agent``; holds no per-question state itself."""

    name = "react"

    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMClient | None = None,
        config: ReactConfig | None = None,
        tools_factory: Callable[[EvidenceStore], ToolExecutor] | None = None,
    ) -> None:
        self.settings = settings
        self.config = config or ReactConfig()
        self._llm = llm or build_llm(settings)
        self._retrieval = Retriever.from_settings(settings)
        self._tools_factory = tools_factory or (
            lambda store: ToolExecutor(settings.database_url, store, retrieval=self._retrieval)
        )

    async def answer(self, question: Question) -> Answer:
        run = _Run(question, self._llm, self.settings, self.config, self._tools_factory)
        try:
            return await asyncio.wait_for(run.execute(), timeout=self.config.timeout_s)
        except TimeoutError:
            run.trace.notes.append(f"timeout after {self.config.timeout_s}s")
            answer = run._refusal("timeout")
            run.write_trace()
            return answer
