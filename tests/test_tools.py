"""Tool contracts: the failure modes the model actually hits."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from agent.tools import EvidenceStore, ToolArgError, ToolExecutor, safe_compute
from pipelines.config import Settings
from pipelines.db import init_schema
from pipelines.fixtures import ingest as ingest_fixtures


@pytest.fixture()
def database_url() -> Iterator[str]:
    url = Settings.from_env().database_url
    try:
        psycopg.connect(url, connect_timeout=2).close()
    except Exception as exc:
        pytest.skip(f"postgres unavailable: {exc}")
    init_schema(url)
    ingest_fixtures(url)
    yield url


def executor(url: str) -> tuple[ToolExecutor, EvidenceStore]:
    store = EvidenceStore()
    return ToolExecutor(url, store), store


def test_compute_whitelist() -> None:
    assert safe_compute("(120-100)/100") == pytest.approx(0.2)
    assert safe_compute("391035/1000") == pytest.approx(391.035)
    for bad in ["__import__('os')", "a + 1", "2 ** 10", "1/0", "x"]:
        with pytest.raises(ToolArgError):
            safe_compute(bad)


def test_search_rejects_chinese_query_without_touching_db() -> None:
    tool = ToolExecutor("postgresql://unused", EvidenceStore())
    outcome = tool.execute(
        "search_filings",
        {"query": "苹果 营收", "tickers": ["AAPL"], "as_of": "2024-12-31"},
    )
    assert outcome.ok is False
    assert outcome.code == "bad_args"
    assert "English" in (outcome.message or "")


def test_compute_tool_returns_value() -> None:
    tool = ToolExecutor("postgresql://unused", EvidenceStore())
    outcome = tool.execute("compute", {"expression": "648125/254453"})
    assert outcome.ok and outcome.data is not None
    assert outcome.data["value"] == pytest.approx(2.5471, rel=1e-3)


def test_search_returns_handles(database_url: str) -> None:
    tool, store = executor(database_url)
    outcome = tool.execute(
        "search_filings",
        {"query": "Apple total net revenue", "tickers": ["AAPL"], "as_of": "2024-12-31"},
    )
    assert outcome.ok and outcome.data is not None
    results = outcome.data["results"]
    assert results
    assert results[0]["handle"].startswith("K")
    assert len(store.handles) == len(results)


def test_get_section_roundtrip(database_url: str) -> None:
    tool, _store = executor(database_url)
    found = tool.execute(
        "search_filings",
        {"query": "Apple revenue", "tickers": ["AAPL"], "as_of": "2024-12-31"},
    )
    assert found.data is not None
    first = found.data["results"][0]
    section = tool.execute("get_section", {"doc_id": first["doc_id"], "item": first["item"]})
    assert section.ok and section.data is not None
    assert section.data["text"]


def test_get_section_slices_inside_full_document(database_url: str) -> None:
    tool, _store = executor(database_url)
    outcome = tool.execute("get_section", {"doc_id": "meridian-10k-fy2024", "item": "8"})
    assert outcome.ok and outcome.data is not None
    assert "Consolidated total revenue" in outcome.data["text"]
    assert outcome.data["chars"][0] > 0  # absolute offset, not 0


def test_xbrl_revenue_exact(database_url: str) -> None:
    tool, _store = executor(database_url)
    outcome = tool.execute(
        "xbrl_fact", {"ticker": "AAPL", "tag_query": "revenue", "as_of": "2024-12-31"}
    )
    assert outcome.ok and outcome.data is not None
    values = [item["value"] for item in outcome.data["matches"]]
    assert 391_035_000_000.0 in values
    assert outcome.data["citation_handle"].startswith("K")


def test_xbrl_alias_finds_capex(database_url: str) -> None:
    tool, _store = executor(database_url)
    outcome = tool.execute(
        "xbrl_fact", {"ticker": "MSFT", "tag_query": "capex", "as_of": "2024-12-31"}
    )
    assert outcome.ok and outcome.data is not None
    tags = {item["tag"] for item in outcome.data["matches"]}
    assert "PaymentsToAcquirePropertyPlantAndEquipment" in tags


def test_xbrl_soft_period_filter(database_url: str) -> None:
    """A guessed period_end must not turn into 'no data'."""
    tool, _store = executor(database_url)
    outcome = tool.execute(
        "xbrl_fact",
        {
            "ticker": "PEP",
            "tag_query": "net income",
            "as_of": "2025-06-30",
            "period_end": "2024-12-31",
        },
    )
    assert outcome.ok and outcome.data is not None
    assert outcome.data["period_end_miss"] is True
    assert outcome.data["matches"]


def test_unknown_tool_and_bad_args(database_url: str) -> None:
    tool, _store = executor(database_url)
    assert tool.execute("nope", {}).code == "unknown_tool"
    assert tool.execute("compute", {"expression": 3}).code == "bad_args"


def test_fixture_corpus_is_queryable(database_url: str) -> None:
    tool, _store = executor(database_url)
    outcome = tool.execute(
        "search_filings",
        {"query": "Northwind revenue 2024", "tickers": ["NORTHWIND"], "as_of": "2025-06-30"},
    )
    assert outcome.ok and outcome.data is not None
    assert outcome.data["results"]
    # Point-in-time: the Aurora amendment is invisible before it was filed.
    early = tool.execute(
        "search_filings",
        {"query": "Aurora revenue restatement", "tickers": ["AURORA"], "as_of": "2024-06-30"},
    )
    assert early.data is not None
    assert all(item["doc_id"] != "aurora-10ka-fy2023" for item in early.data["results"])
    later = tool.execute(
        "search_filings",
        {"query": "Aurora revenue restatement", "tickers": ["AURORA"], "as_of": "2024-12-31"},
    )
    assert later.data is not None
    assert any(item["doc_id"] == "aurora-10ka-fy2023" for item in later.data["results"])


def test_date_validation() -> None:
    tool = ToolExecutor("postgresql://unused", EvidenceStore())
    outcome = tool.execute(
        "search_filings", {"query": "revenue", "tickers": ["AAPL"], "as_of": "2024-13-40"}
    )
    assert outcome.code == "bad_args"
