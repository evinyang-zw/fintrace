"""Per-question trace files: one JSON per run, readable without a database."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from eval.schema import Answer, Question


@dataclass
class TraceStep:
    index: int
    thought: str
    action: str
    args: dict[str, Any]
    ok: bool
    code: str | None
    observation: str
    tool_latency_ms: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    tokens_cached: int = 0
    llm_latency_ms: int = 0
    peak: bool = False
    hedged: bool = False
    hedge_won: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "thought": self.thought,
            "action": self.action,
            "args": self.args,
            "ok": self.ok,
            "code": self.code,
            "observation": self.observation,
            "tool_latency_ms": self.tool_latency_ms,
            "tokens_in": self.tokens_in,
            "tokens_cached": self.tokens_cached,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "llm_latency_ms": self.llm_latency_ms,
            "peak": self.peak,
            "hedged": self.hedged,
            "hedge_won": self.hedge_won,
        }


@dataclass
class Trace:
    trace_id: str
    question: Question
    steps: list[TraceStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    answer: Answer | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "question": self.question.model_dump(mode="json"),
            "meta": self.meta,
            "steps": [step.as_dict() for step in self.steps],
            "notes": self.notes,
            "answer": self.answer.model_dump(mode="json") if self.answer else None,
        }

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.trace_id}.json"
        path.write_text(
            json.dumps(self.as_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return path
