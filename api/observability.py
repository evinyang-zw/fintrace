"""Optional Langfuse tracing for a finished run.

Failure policy: tracing is observability, not correctness. Every call is wrapped
so a Langfuse outage can never fail a user's run or change its answer.

Only one span per run is emitted (input question, output answer, cost/latency/
tier metadata). Per-LLM-call generations would need hooks inside the client; the
run-level span is enough to find *which* runs are slow or expensive, and the
JSON trace under ``data/traces`` still has the per-step detail.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from api.config import ApiConfig

log = logging.getLogger("fintrace.api.langfuse")


class RunTracer:
    def __init__(self, cfg: ApiConfig) -> None:
        self._enabled = bool(cfg.langfuse_public_key and cfg.langfuse_secret_key)
        self._client: Any = None
        self._cfg = cfg
        if not self._enabled:
            return
        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=cfg.langfuse_public_key,
                secret_key=cfg.langfuse_secret_key,
                host=cfg.langfuse_host,
            )
        except Exception as exc:  # pragma: no cover - environment issue
            log.warning("langfuse disabled: %s", exc)
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record(
        self,
        *,
        run_id: str,
        question: str,
        answer: dict[str, Any] | None,
        usage: dict[str, Any] | None,
        status: str,
        latency_ms: int,
        error: str | None = None,
    ) -> None:
        if not self._enabled or self._client is None:
            return
        try:
            trace_id = self._client.create_trace_id(seed=run_id)
            metadata = {
                "status": status,
                "cost_usd": (usage or {}).get("cost_usd"),
                "retrieval_cost_usd": (usage or {}).get("retrieval_cost_usd"),
                "model": (usage or {}).get("model"),
                "tool_calls": (usage or {}).get("tool_calls"),
                "latency_ms": latency_ms,
                "error": error,
            }
            with self._client.start_as_current_observation(
                as_type="span",
                name="fintrace.run",
                trace_context={"trace_id": trace_id},
                input=question,
                output=(answer or {}).get("text"),
                metadata=metadata,
                level="ERROR" if error else "DEFAULT",
            ) as span:
                span.update(output=(answer or {}).get("text"), metadata=metadata)
        except Exception as exc:  # pragma: no cover - never break a run
            log.warning("langfuse record failed: %s", exc)
            self._enabled = False

    def flush(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                self._client.flush()
