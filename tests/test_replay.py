"""Offline replay: three questions answered from cassettes, no network, no cost.

This is the cheap regression net. If a prompt or tool contract changes, the
cassette key changes, the replay misses, and this test fails — which is the
signal to re-record (`FINTRACE_LLM_MODE=record make eval AGENT=react`), not a
reason to delete the test.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from agent.react import ReactAgent, ReactConfig
from eval.runner import load_dataset
from pipelines.config import Settings
from pipelines.db import init_schema
from pipelines.fixtures import ingest as ingest_fixtures

CASSETTE_DIR = Path("tests/cassettes")
CASE_IDS = ("a1-001", "a1-003", "a1-005")


@pytest.fixture()
def database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = Settings.from_env().database_url
    try:
        psycopg.connect(url, connect_timeout=2).close()
    except Exception as exc:
        pytest.skip(f"postgres unavailable: {exc}")
    if not list(CASSETTE_DIR.glob("*.json")):
        pytest.skip("no cassettes recorded")

    init_schema(url)
    # Re-ingesting rewrites chunk ids and can shift tool observations just enough
    # to miss a cassette. Seed once; replay against the same rows afterwards.
    with psycopg.connect(url) as conn:
        seeded = conn.execute(
            "SELECT 1 FROM chunks WHERE accession = 'meridian-10k-fy2024' LIMIT 1"
        ).fetchone()
    if not seeded:
        ingest_fixtures(url)
    monkeypatch.setenv("FINTRACE_LLM_MODE", "replay")
    monkeypatch.setenv("LLM_API_KEY", "replay-only")
    monkeypatch.setenv("DATABASE_URL", url)
    yield url


@pytest.mark.replay
async def test_three_questions_replay_without_network(database_url: str) -> None:
    settings = Settings.from_env()
    agent = ReactAgent(settings, config=ReactConfig(budget_usd=1.0, timeout_s=60))
    questions = {q.id: q for q in load_dataset(Path("eval/datasets/gold_v0.jsonl"))}

    for question_id in CASE_IDS:
        answer = await agent.answer(questions[question_id])
        assert not answer.refused, f"{question_id} refused during replay"
        assert answer.citations, f"{question_id} answered without citations"
        assert answer.usage.cost_usd > 0, f"{question_id} did not read a cassette"

        trace_path = Path(settings.data_dir) / "traces" / f"{answer.trace_id}.json"
        payload = json.loads(trace_path.read_text(encoding="utf-8"))
        assert payload["answer"]["text"]
