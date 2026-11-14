"""API contract tests with a fake agent; no LLM and no MCP process involved."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.config import ApiConfig
from api.main import create_app
from eval.schema import Answer, Citation, Usage


class FakeAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def astream_answer(self, question: Any, on_event: Any = None) -> Answer:
        self.calls += 1
        if on_event is not None:
            await on_event("agent_node", {})
        return Answer(
            text="苹果 FY2024 总净营收为 3910.35 亿美元 [K1]。",
            citations=[
                Citation(
                    accession="AAPL/10-K/FY2024",
                    item_code="8",
                    char_start=0,
                    char_end=10,
                    quote="391,035",
                    handle="K1",
                )
            ],
            usage=Usage(cost_usd=0.001, tokens_in=10, tokens_out=5, tool_calls=1),
            trace_id="fake-trace",
        )

    async def start_review(self, question: Any) -> dict[str, Any]:
        return {
            "status": "awaiting_review",
            "thread_id": "fake-thread",
            "payload": {"draft": {"text": "draft"}},
        }

    async def resume(self, thread_id: str, decision: dict[str, Any]) -> Answer:
        return Answer(text=f"resumed:{decision['action']}", trace_id=thread_id)

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("API_TOKEN", "test-token")
    cfg = ApiConfig(
        database_url=ApiConfig.from_env().database_url,
        api_token="test-token",
        workers=2,
        queue_size=4,
        per_ip_per_minute=100,
        per_ip_per_day=100,
        daily_budget_usd=1.0,
    )
    agent = FakeAgent()
    app = create_app(cfg, agent_factory=lambda: agent, review_agent_factory=lambda: agent)
    with TestClient(app) as client:
        yield client


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _wait(client: TestClient, run_id: str, status: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/runs/{run_id}", headers=_auth()).json()
        if body["status"] == status:
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {status}")


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["queue_capacity"] == 4


def test_ask_requires_token(client: TestClient) -> None:
    response = client.post("/api/ask", json={"question": "苹果营收？", "universe": ["AAPL"]})
    assert response.status_code == 401


def test_ask_runs_and_finishes(client: TestClient) -> None:
    response = client.post(
        "/api/ask",
        headers=_auth(),
        json={"question": "苹果 FY2024 总营收？", "universe": "AAPL"},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    body = _wait(client, run_id, "done")
    assert "3910.35" in body["answer"]["text"]
    assert body["usage"]["cost_usd"] == pytest.approx(0.001)


def test_review_flow(client: TestClient) -> None:
    response = client.post(
        "/api/ask",
        headers=_auth(),
        json={"question": "苹果 FY2024 总营收？", "universe": ["AAPL"], "review": True},
    )
    run_id = response.json()["run_id"]
    body = _wait(client, run_id, "awaiting_review")
    assert body["review_payload"]["draft"]["text"] == "draft"

    approved = client.post(
        f"/api/runs/{run_id}/review", headers=_auth(), json={"action": "approve"}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["answer"]["text"] == "resumed:approve"

    again = client.post(f"/api/runs/{run_id}/review", headers=_auth(), json={"action": "approve"})
    assert again.status_code == 409


def test_stream_ticket(client: TestClient) -> None:
    response = client.post(
        "/api/ask",
        headers=_auth(),
        json={"question": "苹果 FY2024 总营收？", "universe": ["AAPL"]},
    )
    run_id = response.json()["run_id"]
    bad = client.get(f"/api/runs/{run_id}/events?ticket=nope")
    assert bad.status_code == 401
    ticket = response.json()["ticket"]
    with client.stream("GET", f"/api/runs/{run_id}/events?ticket={ticket}") as stream:
        assert stream.status_code == 200
        text = "".join(stream.iter_text())
    assert '"type": "done"' in text


def test_stats(client: TestClient) -> None:
    body = client.get("/api/stats?days=7").json()
    assert body["days"] == 7
    assert "cost_per_question" in body


def test_trace_endpoint_reads_the_run_trace(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FINTRACE_DATA_DIR", str(tmp_path))
    (tmp_path / "traces").mkdir()
    (tmp_path / "traces" / "fake-trace.json").write_text('{"steps": [{"index": 1}]}', "utf-8")

    response = client.post(
        "/api/ask",
        headers=_auth(),
        json={"question": "苹果 FY2024 总营收？", "universe": ["AAPL"]},
    )
    run_id = response.json()["run_id"]
    _wait(client, run_id, "done")

    trace = client.get(f"/api/runs/{run_id}/trace", headers=_auth())
    assert trace.status_code == 200
    assert trace.json()["steps"][0]["index"] == 1


def test_trace_endpoint_requires_auth(client: TestClient) -> None:
    import uuid

    response = client.get(f"/api/runs/{uuid.uuid4()}/trace")
    assert response.status_code == 401
