"""Serializable state for the W4 graph.

The whole point of moving from ``_Run`` to a graph is that the checkpointer can
persist state between processes. That only works if state contains plain data:
no clients, no connections, no ``EvidenceStore`` instances. Handles are stored
as records and rehydrated per node; the store's counters are rebuilt in order.
"""

from __future__ import annotations

from datetime import date
from typing import Any, TypedDict

from agent.tools import EvidenceStore, FactEvidence, Handle
from eval.schema import Question


class GraphState(TypedDict, total=False):
    question: dict[str, Any]
    trace_id: str
    trace_dir: str
    messages: list[dict[str, str]]
    step: int
    status: str  # running | tool | draft | fail | step_limit | budget | done
    pending_action: str
    pending_args: dict[str, Any]
    pending_thought: str
    handles: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    search_calls: list[dict[str, Any]]
    usage: dict[str, Any]
    draft: dict[str, Any] | None
    verification: dict[str, Any] | None
    repairs: int
    notes: list[str]
    trace_steps: list[dict[str, Any]]
    review: dict[str, Any] | None
    answer: dict[str, Any] | None


def empty_usage() -> dict[str, Any]:
    return {
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cached": 0,
        "cost_usd": 0.0,
        "latency_ms": 0,
        "tool_calls": 0,
        "model": "",
        "peak": False,
    }


def initial_state(question: Question, *, messages: list[dict[str, str]]) -> GraphState:
    return GraphState(
        question=question.model_dump(mode="json"),
        messages=messages,
        step=0,
        status="running",
        handles=[],
        facts=[],
        search_calls=[],
        usage=empty_usage(),
        draft=None,
        verification=None,
        repairs=0,
        notes=[],
        trace_steps=[],
        review=None,
        answer=None,
    )


def handle_record(handle: Handle) -> dict[str, Any]:
    return {
        "name": handle.name,
        "accession": handle.accession,
        "item_code": handle.item_code,
        "char_start": handle.char_start,
        "char_end": handle.char_end,
        "quote": handle.quote,
        "label": handle.label,
        "filed_at": handle.filed_at.isoformat(),
    }


def fact_record(fact: FactEvidence) -> dict[str, Any]:
    return {
        "name": fact.name,
        "ticker": fact.ticker,
        "tag": fact.tag,
        "value": fact.value,
        "unit": fact.unit,
        "period_end": fact.period_end.isoformat(),
        "fiscal_period": fact.fiscal_period,
    }


def store_from_state(state: GraphState) -> EvidenceStore:
    """Rebuild the per-run evidence ledger from serializable records.

    Handles are re-added in order, so their names (K1..Kn) come out identical;
    a mismatch means the state was tampered with or reordered, which we want to
    fail loudly on rather than silently renumber citations.
    """
    store = EvidenceStore()
    for record in state.get("handles", []):
        handle = store.add(
            accession=str(record["accession"]),
            item_code=str(record["item_code"]),
            char_start=int(record["char_start"]),
            char_end=int(record["char_end"]),
            quote=str(record["quote"]),
            label=str(record["label"]),
            filed_at=date.fromisoformat(str(record["filed_at"])),
        )
        if handle.name != record["name"]:
            raise ValueError(f"handle order mismatch: {handle.name} != {record['name']}")
    for record in state.get("facts", []):
        fact = store.add_fact(
            ticker=str(record["ticker"]),
            tag=str(record["tag"]),
            value=float(record["value"]),
            unit=str(record["unit"]),
            period_end=date.fromisoformat(str(record["period_end"])),
            fiscal_period=(
                str(record["fiscal_period"]) if record.get("fiscal_period") is not None else None
            ),
        )
        if fact.name != record["name"]:
            raise ValueError(f"fact order mismatch: {fact.name} != {record['name']}")
    store.search_calls.extend(state.get("search_calls", []))
    return store


def state_evidence(store: EvidenceStore) -> dict[str, Any]:
    return {
        "handles": [handle_record(h) for h in store.handles.values()],
        "facts": [fact_record(f) for f in store.facts.values()],
        "search_calls": list(store.search_calls),
    }
