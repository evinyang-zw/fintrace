"""LangGraph orchestration for FinTrace (W4).

The graph is deliberately a *faithful port* of the W2/W3 ReAct loop: the inner
``agent -> tools -> agent`` cycle is unchanged, because that adaptive tool use is
what produced the 95% baseline. What W4 adds is the outer ring: ``verify``,
``repair`` and ``review``. Rewriting the loop as a fixed
``plan -> retrieve -> extract -> compute`` pipeline was rejected in ADR-006.

State stays JSON-serializable so the Postgres checkpointer can persist it and a
different process can resume after an interrupt.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent.graph.state import (
    GraphState,
    empty_usage,
    initial_state,
    state_evidence,
    store_from_state,
)
from agent.llm import EmptyResponseError, LLMClient, LLMError, Message, build_llm
from agent.prompts import (
    PROMPT_VERSION,
    build_finalize_prompt,
    build_system_prompt,
    build_task_prompt,
)
from agent.react import REFUSAL_PREFIX, parse_action
from agent.retrieval import Retriever
from agent.tools import (
    TOOL_SPECS,
    EvidenceStore,
    ToolExecutor,
    citation_from,
    evidence_citations,
)
from agent.trace import Trace, TraceStep
from eval.citation import CitationChecker, extract_claims
from eval.schema import Answer, Citation, FactCitation, Question, Usage
from pipelines.config import Settings

REPAIR_PROMPT = (
    "上一条不是合法的 action JSON。只输出一个 JSON 对象，"
    '格式：{"thought": "...", "action": "工具名或 final_answer", "action_input": {...}}。'
    "不要 Markdown，不要额外文字。"
)

_CLAIM_VERIFY_THRESHOLD = 0.8


@dataclass(frozen=True, slots=True)
class GraphConfig:
    max_steps: int = 7
    max_tokens_per_step: int = 1200
    budget_usd: float = 0.25
    timeout_s: float = 150.0
    review_mode: str = "never"  # never | always | on_failure
    verify_semantic: bool = True
    max_repairs: int = 2
    recursion_limit: int = 80


@dataclass(frozen=True, slots=True)
class BackendResult:
    ok: bool
    code: str | None
    observation: str


class ToolBackend(Protocol):
    async def execute(
        self, store: EvidenceStore, name: str, args: dict[str, Any]
    ) -> BackendResult: ...


@dataclass
class InProcessBackend:
    database_url: str
    retrieval: Retriever

    async def execute(self, store: EvidenceStore, name: str, args: dict[str, Any]) -> BackendResult:
        executor = ToolExecutor(self.database_url, store, retrieval=self.retrieval)
        outcome = await asyncio.to_thread(executor.execute, name, args)
        return BackendResult(outcome.ok, outcome.code, outcome.to_observation())


@dataclass
class GraphDeps:
    settings: Settings
    llm: LLMClient
    backend: ToolBackend
    config: GraphConfig
    checker: CitationChecker | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = 2600) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…[{len(text) - limit} chars elided]"


def _add_usage(usage: dict[str, Any], result: Any) -> dict[str, Any]:
    merged = dict(usage)
    merged["tokens_in"] = int(merged.get("tokens_in", 0)) + result.tokens_in
    merged["tokens_cached"] = int(merged.get("tokens_cached", 0)) + result.tokens_cached
    merged["tokens_out"] = int(merged.get("tokens_out", 0)) + result.tokens_out
    merged["cost_usd"] = float(merged.get("cost_usd", 0.0)) + result.cost_usd
    merged["latency_ms"] = int(merged.get("latency_ms", 0)) + result.latency_ms
    merged["model"] = result.model or merged.get("model", "")
    merged["peak"] = bool(merged.get("peak", False)) or result.peak
    return merged


def _step(
    *,
    index: int,
    action: str,
    args: dict[str, Any],
    thought: str,
    ok: bool,
    code: str | None = None,
    observation: str = "",
    tool_latency_ms: int = 0,
    result: Any | None = None,
) -> dict[str, Any]:
    return {
        "index": index,
        "thought": thought,
        "action": action,
        "args": args,
        "ok": ok,
        "code": code,
        "observation": _truncate(observation),
        "tool_latency_ms": tool_latency_ms,
        "tokens_in": getattr(result, "tokens_in", 0) if result else 0,
        "tokens_cached": getattr(result, "tokens_cached", 0) if result else 0,
        "tokens_out": getattr(result, "tokens_out", 0) if result else 0,
        "cost_usd": getattr(result, "cost_usd", 0.0) if result else 0.0,
        "llm_latency_ms": getattr(result, "latency_ms", 0) if result else 0,
        "peak": bool(getattr(result, "peak", False)) if result else False,
        "hedged": bool(getattr(result, "hedged", False)) if result else False,
        "hedge_won": bool(getattr(result, "hedge_won", False)) if result else False,
    }


def _usage_from_state(state: GraphState) -> Usage:
    raw = state.get("usage") or empty_usage()
    return Usage(
        tokens_in=int(raw.get("tokens_in", 0)),
        tokens_out=int(raw.get("tokens_out", 0)),
        tokens_cached=int(raw.get("tokens_cached", 0)),
        model=str(raw.get("model", "")),
        cost_usd=round(float(raw.get("cost_usd", 0.0)), 8),
        peak=bool(raw.get("peak", False)),
        latency_ms=int(raw.get("latency_ms", 0)),
        tool_calls=int(raw.get("tool_calls", 0)),
    )


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------


def build_nodes(deps: GraphDeps) -> dict[str, Callable[..., Any]]:
    cfg = deps.config

    async def agent_node(state: GraphState) -> GraphState:
        usage = dict(state.get("usage") or empty_usage())
        notes = list(state.get("notes") or [])
        steps = list(state.get("trace_steps") or [])
        step = int(state.get("step", 0))

        if step >= cfg.max_steps:
            notes.append(f"step limit {cfg.max_steps} reached")
            return {"status": "step_limit", "notes": notes}
        if float(usage.get("cost_usd", 0.0)) >= cfg.budget_usd:
            notes.append(f"budget exhausted at ${usage.get('cost_usd', 0.0):.4f}")
            return {"status": "budget", "notes": notes}

        messages = [Message(cast(Any, m["role"]), m["content"]) for m in state.get("messages", [])]
        try:
            result = await deps.llm.complete(
                messages,
                max_tokens=cfg.max_tokens_per_step,
                json_mode=True,
                temperature=float(deps.settings.llm_temperature),
            )
        except EmptyResponseError as exc:
            notes.append(f"step {step + 1}: empty response, retry once: {exc}")
            try:
                result = await deps.llm.complete(
                    messages,
                    max_tokens=cfg.max_tokens_per_step,
                    json_mode=True,
                    temperature=float(deps.settings.llm_temperature),
                )
            except LLMError as retry_exc:
                notes.append(f"step {step + 1}: empty again: {retry_exc}")
                return {"status": "fail", "notes": notes, "usage": usage}
        except LLMError as exc:
            notes.append(f"step {step + 1}: llm error: {exc}")
            return {"status": "fail", "notes": notes, "usage": usage}

        usage = _add_usage(usage, result)
        appended = [*state.get("messages", []), {"role": "assistant", "content": result.text}]
        parsed = parse_action(result.text)
        index = step + 1

        if parsed is None:
            notes.append(f"step {index}: unparseable reply")
            steps.append(
                _step(
                    index=index,
                    action="(unparseable)",
                    args={},
                    thought="",
                    ok=False,
                    code="parse_error",
                    observation=_truncate(result.text, 500),
                    result=result,
                )
            )
            appended.append({"role": "user", "content": REPAIR_PROMPT})
            return {
                "messages": appended,
                "step": index,
                "status": "running",
                "notes": notes,
                "trace_steps": steps,
                "usage": usage,
            }

        action, args, thought = parsed
        if action == "final_answer":
            steps.append(
                _step(
                    index=index,
                    action="final_answer",
                    args={"citations": args.get("citations", [])},
                    thought=thought,
                    ok=True,
                    observation=_truncate(str(args.get("text", "")), 500),
                    result=result,
                )
            )
            return {
                "messages": appended,
                "step": index,
                "status": "draft",
                "draft": args,
                "notes": notes,
                "trace_steps": steps,
                "usage": usage,
            }
        if action == "fail":
            notes.append(f"step {index}: agent declared failure")
            return {"status": "fail", "notes": notes, "step": index, "usage": usage}

        steps.append(
            _step(
                index=index,
                action=action,
                args=args,
                thought=thought,
                ok=True,
                code="requested",
                result=result,
            )
        )
        return {
            "messages": appended,
            "step": index,
            "status": "tool",
            "pending_action": action,
            "pending_args": args,
            "pending_thought": thought,
            "notes": notes,
            "trace_steps": steps,
            "usage": usage,
        }

    async def tools_node(state: GraphState) -> GraphState:
        store = store_from_state(state)
        action = str(state.get("pending_action", ""))
        args = dict(state.get("pending_args") or {})
        steps = list(state.get("trace_steps") or [])
        notes = list(state.get("notes") or [])
        usage = dict(state.get("usage") or empty_usage())

        started = time.perf_counter()
        try:
            outcome = await deps.backend.execute(store, action, args)
        except Exception as exc:  # a tool must never kill the graph
            outcome = BackendResult(False, "internal", f"ERROR[internal]: {exc}")
        elapsed = int((time.perf_counter() - started) * 1000)
        usage["tool_calls"] = int(usage.get("tool_calls", 0)) + 1

        # The tool step shares the LLM step index (one decision, one action).
        index = max(int(state.get("step", 1)), 1)
        steps.append(
            _step(
                index=index,
                action=action,
                args=args,
                thought=str(state.get("pending_thought", "")),
                ok=outcome.ok,
                code=outcome.code,
                observation=outcome.observation,
                tool_latency_ms=elapsed,
            )
        )
        messages = [
            *state.get("messages", []),
            {
                "role": "user",
                "content": (
                    f"TOOL RESULT {action}:\n{_truncate(outcome.observation)}\n"
                    f"当前可用句柄: {store.known()}"
                ),
            },
        ]
        result: dict[str, Any] = {
            "messages": messages,
            "status": "running",
            "trace_steps": steps,
            "notes": notes,
            "usage": usage,
        }
        result.update(state_evidence(store))
        return cast(GraphState, result)

    async def verify_node(state: GraphState) -> GraphState:
        store = store_from_state(state)
        draft = dict(state.get("draft") or {})
        text = str(draft.get("text") or "")
        problems: list[str] = []
        report: dict[str, Any] | None = None
        score: float | None = None

        # A refusal is a legitimate outcome: there is nothing to verify
        # semantically, and failing it here would send the agent into two
        # pointless repair rounds (and mark a correct refusal as "failed").
        if bool(draft.get("refused")) or text.startswith(REFUSAL_PREFIX):
            return {
                "verification": {
                    "passed": True,
                    "refusal": True,
                    "problems": [],
                    "score": None,
                    "report": None,
                    "uncited": 0,
                    "claims": 0,
                }
            }

        used = list(dict.fromkeys(re.findall(r"\[([KF]\d+)\]", text)))
        unknown = [
            handle for handle in used if handle.startswith("K") and handle not in store.handles
        ] + [handle for handle in used if handle.startswith("F") and handle not in store.facts]
        if unknown:
            problems.append(f"引用了不存在的句柄：{', '.join(unknown)}")

        claims, uncited = extract_claims(text) if text else ([], 0)
        if not text:
            problems.append("最终答案为空")

        if deps.checker is not None and text and not unknown:
            partial = _answer_from_parts(state, store)
            try:
                citation_report = await deps.checker.check(
                    Question.model_validate(state["question"]), partial
                )
            except Exception as exc:  # verification must not kill the run
                problems.append(f"引用校验器失败：{type(exc).__name__}: {exc}")
            else:
                score = citation_report.score
                report = {
                    "score": citation_report.score,
                    "supported": citation_report.supported,
                    "partial": citation_report.partial,
                    "unsupported": citation_report.unsupported,
                    "uncited": citation_report.uncited,
                }
                if citation_report.unsupported:
                    problems.append(f"{citation_report.unsupported} 条引用完全不支持结论")
                if score < _CLAIM_VERIFY_THRESHOLD:
                    problems.append(f"引用支撑率 {score:.2f} 低于阈值 {_CLAIM_VERIFY_THRESHOLD}")

        verdict = {
            "passed": not problems,
            "problems": problems,
            "score": score,
            "report": report,
            "uncited": uncited,
            "claims": len(claims),
        }
        return {"verification": verdict}

    async def repair_node(state: GraphState) -> GraphState:
        verdict = dict(state.get("verification") or {})
        repairs = int(state.get("repairs", 0)) + 1
        notes = list(state.get("notes") or [])
        problems = verdict.get("problems") or ["答案未通过校验"]
        notes.append(f"repair {repairs}: {len(problems)} problem(s)")
        feedback = (
            "你的答案没通过校验，必须修正后重新输出 final_answer。\n"
            "问题：\n"
            + "\n".join(f"- {item}" for item in problems)
            + "\n只使用已返回的句柄；无法支撑的句子删掉，或换成能支撑它的引用。"
        )
        messages = [*state.get("messages", []), {"role": "user", "content": feedback}]
        return {
            "messages": messages,
            "status": "running",
            "draft": None,
            "verification": None,
            "repairs": repairs,
            "notes": notes,
        }

    async def review_node(state: GraphState) -> GraphState:
        verdict = dict(state.get("verification") or {})
        review = {"decision": "auto", "editor": "none"}
        if cfg.review_mode == "never":
            review["reason"] = "review disabled"
            return {"status": "done", "review": review}

        payload = {
            "question": state.get("question"),
            "draft": state.get("draft"),
            "verification": verdict,
            "trace_id": state.get("trace_id"),
        }
        decision = interrupt(payload)
        if isinstance(decision, dict):
            action = str(decision.get("action", "approve"))
            review = {"decision": action, "editor": "human"}
            if action == "edit" and decision.get("text"):
                draft = dict(state.get("draft") or {})
                draft["text"] = str(decision["text"])
                return {"status": "done", "review": review, "draft": draft}
            if action == "reject":
                return {
                    "status": "done",
                    "review": review,
                    "draft": {
                        "text": f"{REFUSAL_PREFIX}：人工复核未通过，暂不发布该结论。",
                        "citations": [],
                    },
                }
        return {"status": "done", "review": review}

    async def finalize_node(state: GraphState) -> GraphState:
        status = str(state.get("status", ""))
        draft = state.get("draft")
        notes = list(state.get("notes") or [])
        steps = list(state.get("trace_steps") or [])
        usage = dict(state.get("usage") or empty_usage())
        messages = list(state.get("messages") or [])

        # Parity with the hand-written loop: on step/budget/LLM failure it gets
        # one last no-tools call to salvage whatever evidence it already saw.
        # Without this, a question that needed one more step becomes a refusal,
        # which is exactly the W4 regression we measured against react.
        if not draft and status in {"fail", "step_limit", "budget"}:
            reason = {
                "fail": "LLM 调用失败",
                "step_limit": "已达到步数上限",
                "budget": "已达到预算上限",
            }[status]
            if float(usage.get("cost_usd", 0.0)) < cfg.budget_usd * 1.2:
                try:
                    result = await deps.llm.complete(
                        [
                            *[Message(cast(Any, m["role"]), m["content"]) for m in messages],
                            Message("user", build_finalize_prompt(reason)),
                        ],
                        max_tokens=cfg.max_tokens_per_step,
                        json_mode=True,
                        temperature=float(deps.settings.llm_temperature),
                    )
                except LLMError as exc:
                    notes.append(f"finalize({status}): {exc}")
                else:
                    usage = _add_usage(usage, result)
                    messages.append({"role": "assistant", "content": result.text})
                    parsed = parse_action(result.text)
                    if parsed is not None and parsed[0] == "final_answer":
                        draft = parsed[1]
                        steps.append(
                            _step(
                                index=int(state.get("step", 0)) + 1,
                                action="finalize_prompt",
                                args={"reason": status},
                                thought=parsed[2],
                                ok=True,
                                observation=_truncate(result.text, 400),
                                result=result,
                            )
                        )
                        notes.append(f"finalize({status}): salvaged a final_answer")
                    else:
                        notes.append(f"finalize({status}): reply was not final_answer")

        merged: GraphState = cast(
            GraphState,
            {
                **state,
                "draft": draft,
                "messages": messages,
                "notes": notes,
                "trace_steps": steps,
                "usage": usage,
            },
        )
        store = store_from_state(merged)
        answer = _build_answer(merged, store, status)
        _write_trace(merged, answer, status)
        return {
            "status": "done",
            "answer": answer.model_dump(mode="json"),
            "notes": notes,
            "trace_steps": steps,
            "usage": usage,
            "draft": draft,
        }

    def route_after_agent(state: GraphState) -> str:
        status = state.get("status")
        if status == "tool":
            return "tools"
        if status == "draft":
            return "verify"
        if status in {"fail", "step_limit", "budget"}:
            return "finalize"
        return "agent"

    def route_after_verify(state: GraphState) -> str:
        verdict = state.get("verification") or {}
        passed = bool(verdict.get("passed"))
        repairs = int(state.get("repairs", 0))
        if passed:
            return "review" if cfg.review_mode == "always" else "finalize"
        if repairs < cfg.max_repairs:
            return "repair"
        return "review" if cfg.review_mode != "never" else "finalize"

    return {
        "agent": agent_node,
        "tools": tools_node,
        "verify": verify_node,
        "repair": repair_node,
        "review": review_node,
        "finalize": finalize_node,
        "route_after_agent": route_after_agent,
        "route_after_verify": route_after_verify,
    }


# ---------------------------------------------------------------------------
# answer / trace construction
# ---------------------------------------------------------------------------


def _answer_from_parts(state: GraphState, store: EvidenceStore) -> Answer:
    """A partial Answer used only for verification, not returned to callers."""
    draft = dict(state.get("draft") or {})
    text = str(draft.get("text") or "")
    citations, facts = _collect_citations(text, draft, store)
    return Answer(
        text=text,
        citations=citations,
        facts=facts,
        evidence=evidence_citations(store),
        retrieval=list(store.search_calls),
        usage=_usage_from_state(state),
        trace_id=_trace_id(state),
    )


def _collect_citations(
    text: str, draft: dict[str, Any], store: EvidenceStore
) -> tuple[list[Citation], list[FactCitation]]:
    names: list[str] = []
    raw = draft.get("citations")
    if isinstance(raw, list):
        names.extend(str(item) for item in raw)
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
        if (fact := store.facts.get(name)) is not None
    ]

    citations: list[Citation] = []
    seen: set[tuple[str, str, int, int]] = set()
    for name in names:
        handle = store.get(name)
        if handle is None:
            continue
        key = (handle.accession, handle.item_code, handle.char_start, handle.char_end)
        if key in seen:
            continue
        seen.add(key)
        citations.append(citation_from(handle))
    return citations, facts


def _build_answer(state: GraphState, store: EvidenceStore, status: str) -> Answer:
    draft = dict(state.get("draft") or {})
    text = str(draft.get("text") or "").strip()
    citations, facts = _collect_citations(text, draft, store)

    if status in {"fail", "step_limit", "budget"} and not text:
        reason = {
            "fail": "llm_error",
            "step_limit": "step_limit",
            "budget": "budget_exhausted",
        }.get(status, status)
        return Answer(
            text=f"{REFUSAL_PREFIX}：本次运行未能取得足够证据（{reason}）。",
            citations=[],
            evidence=evidence_citations(store),
            retrieval=list(store.search_calls),
            refused=True,
            refusal_reason=reason,
            usage=_usage_from_state(state),
            trace_id=_trace_id(state),
        )

    refused = bool(draft.get("refused")) or not text or text.startswith(REFUSAL_PREFIX)
    verification = state.get("verification") or {}
    extra: dict[str, Any] = {}
    if verification and not verification.get("passed") and not refused:
        extra["verification_failed"] = True
    return Answer(
        text=text or f"{REFUSAL_PREFIX}：没有可用的最终答案。",
        citations=citations,
        facts=facts,
        evidence=evidence_citations(store),
        retrieval=list(store.search_calls),
        refused=refused,
        refusal_reason="insufficient_evidence" if refused else None,
        usage=_usage_from_state(state),
        trace_id=_trace_id(state),
        **extra,
    )


def _trace_id(state: GraphState) -> str:
    return str(state.get("trace_id") or "")


def _write_trace(state: GraphState, answer: Answer, status: str) -> None:
    """Write the same JSON shape as the hand-written loop, plus a graph block."""
    try:
        question = Question.model_validate(state["question"])
        trace = Trace(
            trace_id=answer.trace_id,
            question=question,
            meta={
                "agent": "graph",
                "prompt_version": PROMPT_VERSION,
                "status": status,
                "nodes": ["agent", "tools", "verify", "repair", "review", "finalize"],
                "repairs": state.get("repairs", 0),
                "verification": state.get("verification"),
                "review": state.get("review"),
            },
            notes=list(state.get("notes") or []),
        )
        for raw in state.get("trace_steps") or []:
            trace.steps.append(
                TraceStep(
                    index=int(raw.get("index", 0)),
                    thought=str(raw.get("thought", "")),
                    action=str(raw.get("action", "")),
                    args=dict(raw.get("args") or {}),
                    ok=bool(raw.get("ok")),
                    code=raw.get("code"),
                    observation=str(raw.get("observation", "")),
                    tool_latency_ms=int(raw.get("tool_latency_ms", 0)),
                    tokens_in=int(raw.get("tokens_in", 0)),
                    tokens_cached=int(raw.get("tokens_cached", 0)),
                    tokens_out=int(raw.get("tokens_out", 0)),
                    cost_usd=float(raw.get("cost_usd", 0.0)),
                    llm_latency_ms=int(raw.get("llm_latency_ms", 0)),
                    peak=bool(raw.get("peak", False)),
                    hedged=bool(raw.get("hedged", False)),
                    hedge_won=bool(raw.get("hedge_won", False)),
                )
            )
        trace.answer = answer
    except Exception as exc:  # tracing must never break evaluation
        print(f"warning: could not build trace: {exc}")
        return
    try:
        base = Path(str(state.get("trace_dir") or "data")) / "traces"
        trace.write(base)
    except Exception as exc:
        print(f"warning: could not write trace {answer.trace_id}: {exc}")


# ---------------------------------------------------------------------------
# graph builder + agent
# ---------------------------------------------------------------------------


def build_graph(deps: GraphDeps, checkpointer: Any | None = None) -> Any:
    nodes = build_nodes(deps)
    graph: StateGraph[GraphState] = StateGraph(GraphState)
    graph.add_node("agent", nodes["agent"])
    graph.add_node("tools", nodes["tools"])
    graph.add_node("verify", nodes["verify"])
    graph.add_node("repair", nodes["repair"])
    graph.add_node("review", nodes["review"])
    graph.add_node("finalize", nodes["finalize"])

    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        nodes["route_after_agent"],
        {"agent": "agent", "tools": "tools", "verify": "verify", "finalize": "finalize"},
    )
    graph.add_edge("tools", "agent")
    graph.add_conditional_edges(
        "verify",
        nodes["route_after_verify"],
        {"repair": "repair", "review": "review", "finalize": "finalize"},
    )
    graph.add_edge("repair", "agent")
    graph.add_edge("review", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


class GraphAgent:
    """Satisfies ``eval.schema.Agent``; the graph adds verify/repair/review."""

    name = "graph"

    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMClient | None = None,
        config: GraphConfig | None = None,
        backend: ToolBackend | str | None = None,
        checkpointer: str = "postgres",
        checker: CitationChecker | None = None,
    ) -> None:
        self.settings = settings
        self.config = config or GraphConfig()
        self._llm = llm or build_llm(settings)
        self._retrieval = Retriever.from_settings(settings)
        self._backend: ToolBackend
        if isinstance(backend, str):
            self._backend = self._make_backend(backend)
        elif backend is not None:
            self._backend = backend
        else:
            self._backend = self._make_backend(os.environ.get("FINTRACE_TOOL_BACKEND", "inproc"))
        self._checker = (
            checker
            if checker is not None
            else (CitationChecker(settings) if self.config.verify_semantic else None)
        )
        self._checkpointer_mode = checkpointer
        self._stack = AsyncExitStack()
        self._deps = GraphDeps(
            settings=settings,
            llm=self._llm,
            backend=self._backend,
            config=self.config,
            checker=self._checker,
        )
        self._graph: Any | None = None

    def _make_backend(self, mode: str) -> ToolBackend:
        if mode == "mcp":
            from agent.mcp_backend import MCPBackend

            return MCPBackend()
        return InProcessBackend(self.settings.database_url, self._retrieval)

    async def _ensure_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        if self._checkpointer_mode == "memory":
            saver: Any = MemorySaver()
        else:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            saver = await self._stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(self.settings.database_url)
            )
            await saver.setup()
        self._graph = build_graph(self._deps, saver)
        return self._graph

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self.config.recursion_limit,
        }

    def _new_run(self, question: Question) -> tuple[str, GraphState]:
        trace_id = f"{datetime.now():%Y%m%d-%H%M%S}-{question.id}-{uuid4().hex[:6]}"
        messages = [
            {"role": "system", "content": build_system_prompt(TOOL_SPECS, self.config.max_steps)},
            {"role": "user", "content": build_task_prompt(question, self.config.max_steps)},
        ]
        state = initial_state(question, messages=messages)
        state["trace_id"] = trace_id
        state["trace_dir"] = str(Path(self.settings.data_dir))
        state["review"] = {"mode": self.config.review_mode}
        return trace_id, state

    async def answer(self, question: Question) -> Answer:
        graph = await self._ensure_graph()
        trace_id, state = self._new_run(question)
        try:
            final = await asyncio.wait_for(
                graph.ainvoke(state, self._config(trace_id)),
                timeout=self.config.timeout_s,
            )
        except TimeoutError:
            return Answer(
                text=f"{REFUSAL_PREFIX}：运行超时（{self.config.timeout_s}s）。",
                refused=True,
                refusal_reason="timeout",
                trace_id=trace_id,
            )
        payload = final.get("answer")
        if not payload:
            return Answer(
                text=f"{REFUSAL_PREFIX}：图未产出最终答案。",
                refused=True,
                refusal_reason="graph_without_answer",
                trace_id=trace_id,
            )
        return Answer.model_validate(payload)

    async def start_review(self, question: Question) -> dict[str, Any]:
        """Run until the review interrupt (or completion) and return a handle."""
        graph = await self._ensure_graph()
        trace_id, state = self._new_run(question)
        final = await graph.ainvoke(state, self._config(trace_id))
        interrupts = final.get("__interrupt__")
        if interrupts:
            return {
                "status": "awaiting_review",
                "thread_id": trace_id,
                "payload": interrupts[0].value
                if hasattr(interrupts[0], "value")
                else interrupts[0],
            }
        answer = final.get("answer")
        return {
            "status": "done",
            "thread_id": trace_id,
            "answer": Answer.model_validate(answer) if answer else None,
        }

    async def resume(self, thread_id: str, decision: dict[str, Any]) -> Answer:
        graph = await self._ensure_graph()
        final = await graph.ainvoke(Command(resume=decision), self._config(thread_id))
        payload = final.get("answer")
        if not payload:
            return Answer(
                text=f"{REFUSAL_PREFIX}：恢复后未产出答案。",
                refused=True,
                refusal_reason="resume_without_answer",
                trace_id=thread_id,
            )
        return Answer.model_validate(payload)

    async def aclose(self) -> None:
        close = getattr(self._backend, "aclose", None)
        if close is not None:
            await close()
        await self._stack.aclose()
        self._graph = None

    def close(self) -> None:
        """Kept for callers that cannot await; prefer ``aclose``."""
        self._graph = None
