"""MCP contract tests: schema, errors, handle ownership, read-only server.

These talk to a real ``mcp_server`` subprocess over stdio. They need Postgres
(the CI service and ``make up`` both provide it) but no network and no LLM.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from agent.mcp_backend import MCPBackend, _remap
from agent.tools import EvidenceStore
from pipelines.config import Settings
from pipelines.db import init_schema
from pipelines.fixtures import ingest as ingest_fixtures
from tests.test_tools import _cleanup, _seed

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {"search_filings", "get_section", "xbrl_fact", "compute"}
TICKER = "TSTCO"


@asynccontextmanager
async def _client():  # type: ignore[no-untyped-def]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        cwd=str(ROOT),
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PGOPTIONS": "-c default_transaction_read_only=on",
        },
    )
    async with Client(params) as client:
        yield client


@pytest.mark.asyncio
async def test_tool_list_matches_the_agent_specs() -> None:
    async with _client() as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
    for tool in tools.tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.input_schema.get("properties"), f"{tool.name} has no schema"


@pytest.mark.asyncio
async def test_compute_returns_structured_observation() -> None:
    async with _client() as client:
        result = await client.call_tool("compute", {"expression": "(120-100)/100"})
    payload = result.structured_content or {}
    assert payload["ok"] is True
    assert "0.2" in payload["observation"]


@pytest.mark.asyncio
async def test_bad_arguments_return_a_structured_error_not_a_crash() -> None:
    async with _client() as client:
        result = await client.call_tool("xbrl_fact", {"ticker": "AAPL"})
    payload = result.structured_content or {}
    assert result.is_error or payload.get("ok") is False


@pytest.fixture()
def seeded_db() -> None:
    """CI has an empty warehouse; seed the same tiny company test_tools uses."""
    url = Settings.from_env().database_url
    init_schema(url)
    ingest_fixtures(url)
    _cleanup(url)
    _seed(url)
    yield
    _cleanup(url)


@pytest.mark.asyncio
async def test_xbrl_fact_returns_evidence_records_without_handles(seeded_db: None) -> None:
    async with _client() as client:
        result = await client.call_tool(
            "xbrl_fact", {"ticker": TICKER, "tag_query": "revenue", "as_of": "2024-12-31"}
        )
    payload = result.structured_content or {}
    assert payload["ok"] is True
    assert payload["facts"], "expected at least one XBRL fact"
    # The server must stay stateless: names are F1.. only inside this response,
    # never a global counter the client could depend on.
    assert all(record["name"].startswith("F") for record in payload["facts"])


@pytest.mark.asyncio
async def test_client_assigns_handles_and_remaps_observation(seeded_db: None) -> None:
    backend = MCPBackend()
    store = EvidenceStore()
    try:
        outcome = await backend.execute(
            store, "xbrl_fact", {"ticker": TICKER, "tag_query": "revenue", "as_of": "2024-12-31"}
        )
    finally:
        await backend.aclose()

    assert outcome.ok is True
    assert list(store.facts), "client did not rehydrate facts"
    for name in store.facts:
        assert f"[{name}]" in outcome.observation or f'"{name}"' in outcome.observation


@pytest.mark.asyncio
async def test_timeout_is_reported_as_a_tool_error() -> None:
    backend = MCPBackend(timeout_s=0.001)
    store = EvidenceStore()
    try:
        outcome = await backend.execute(store, "compute", {"expression": "1+1"})
    finally:
        await backend.aclose()
    assert outcome.ok is False
    assert outcome.code == "timeout"


def test_server_is_read_only_by_source() -> None:
    source = (ROOT / "mcp_server" / "__main__.py").read_text(encoding="utf-8").upper()
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE "):
        assert forbidden not in source


def test_remap_rewrites_bracket_and_json_handles() -> None:
    assert _remap("[K1] then [K2]", {"K1": "K4", "K2": "K5"}) == "[K4] then [K5]"
    assert _remap('{"handle": "K1"}', {"K1": "K7"}) == '{"handle": "K7"}'


@pytest.mark.asyncio
async def test_every_tool_rejects_missing_arguments() -> None:
    """4 tools x the protocol-level contract: missing args must not crash."""
    async with _client() as client:
        results = {}
        for name in EXPECTED_TOOLS:
            result = await client.call_tool(name, {})
            results[name] = result
    for name, result in results.items():
        assert result.is_error, f"{name} accepted an empty argument object"
        text = "".join(getattr(item, "text", "") for item in result.content or [])
        assert "required" in text.lower() or "validation" in text.lower()


@pytest.mark.asyncio
async def test_not_found_paths_are_structured_errors(seeded_db: None) -> None:
    async with _client() as client:
        cases = [
            ("search_filings", {"query": "revenue", "tickers": ["NOPE"], "as_of": "2024-12-31"}),
            ("get_section", {"doc_id": "missing-doc", "item": "7"}),
            ("xbrl_fact", {"ticker": "NOPE", "tag_query": "revenue", "as_of": "2024-12-31"}),
        ]
        for name, args in cases:
            result = await client.call_tool(name, args)
            payload = result.structured_content or {}
            assert payload["ok"] is False, f"{name} should fail on unknown input"
            assert payload["code"] == "not_found", f"{name} returned {payload['code']}"


@pytest.mark.asyncio
async def test_empty_search_is_ok_not_an_error(seeded_db: None) -> None:
    async with _client() as client:
        result = await client.call_tool(
            "search_filings",
            {"query": "zzz-not-in-corpus-zzz", "tickers": [TICKER], "as_of": "2024-12-31"},
        )
    payload = result.structured_content or {}
    assert payload["ok"] is True
    assert payload["observation"]


def test_schemas_match_the_agent_tool_specs() -> None:
    from agent.tools import TOOL_SPECS

    expected = {spec["name"]: set(spec["parameters"].get("properties", {})) for spec in TOOL_SPECS}
    import anyio

    async def collect() -> dict[str, set[str]]:
        async with _client() as client:
            tools = await client.list_tools()
        return {tool.name: set(tool.input_schema.get("properties", {})) for tool in tools.tools}

    actual = anyio.run(collect)
    assert actual == expected
