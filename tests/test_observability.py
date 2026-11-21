"""Offline contract test at the Langfuse seam.

The W5 bug (empty Langfuse input) lived one layer above this: ``WorkerPool``
passed the Answer object where the tracer expected a question string. The
caller-side regression test lives in ``test_worker_trace.py``; this one pins the
other half of the contract — whatever the caller passes must reach the Langfuse
SDK as ``input``/``output`` attributes, with no network involved.
"""

from __future__ import annotations

from typing import Any

from api.config import ApiConfig
from api.observability import RunTracer


class _Span:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class _Context:
    def __init__(self, span: _Span) -> None:
        self._span = span

    def __enter__(self) -> _Span:
        return self._span

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeLangfuse:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None
        self.span = _Span()

    def create_trace_id(self, seed: str) -> str:
        return f"trace-{seed}"

    def start_as_current_observation(self, **kwargs: Any) -> _Context:
        self.kwargs = kwargs
        return _Context(self.span)


def _tracer() -> tuple[RunTracer, _FakeLangfuse]:
    tracer = RunTracer(ApiConfig(database_url="sqlite://", api_token="test-token"))
    client = _FakeLangfuse()
    # Exercise the disabled-by-default seam without network or real keys.
    tracer._enabled = True
    tracer._client = client
    return tracer, client


def test_tracer_sends_question_as_input_and_answer_as_output() -> None:
    tracer, client = _tracer()
    tracer.record(
        run_id="run-1",
        question="苹果 FY2024 的总净营收是多少美元？",
        answer={"text": "391,035 百万美元 [F1]"},
        usage={"model": "deepseek-flash", "cost_usd": 0.00019, "tool_calls": 1},
        status="done",
        latency_ms=2100,
    )
    assert client.kwargs is not None
    assert client.kwargs["input"] == "苹果 FY2024 的总净营收是多少美元？"
    assert client.kwargs["output"] == "391,035 百万美元 [F1]"
    assert client.kwargs["name"] == "fintrace.run"
    metadata = client.kwargs["metadata"]
    assert metadata["status"] == "done"
    assert metadata["model"] == "deepseek-flash"
    assert metadata["cost_usd"] == 0.00019
    assert client.span.updates[0]["output"] == "391,035 百万美元 [F1]"


def test_tracer_keeps_input_on_failed_run() -> None:
    tracer, client = _tracer()
    tracer.record(
        run_id="run-2",
        question="微软 FY2024 的 Microsoft Cloud 收入？",
        answer=None,
        usage=None,
        status="failed",
        latency_ms=500,
        error="timeout",
    )
    assert client.kwargs is not None
    assert client.kwargs["input"] == "微软 FY2024 的 Microsoft Cloud 收入？"
    assert client.kwargs["output"] is None
    assert client.kwargs["level"] == "ERROR"
    assert client.kwargs["metadata"]["error"] == "timeout"
