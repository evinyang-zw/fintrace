"""Interface equivalence: in-process tools vs the MCP server.

This is the cheap, deterministic version of "does the process boundary change
anything": same warehouse, same retrieval tier, same arguments — the observation
the agent sees must be identical (handle names included, because both start from
an empty store and replay the same calls in order).

It is stronger evidence than another eval seed: an LLM eval only shows no
*visible* regression at n≈45, while this catches silent differences such as the
server forgetting ``RETRIEVAL_TIER`` and falling back to BM25.
"""

from __future__ import annotations

import pytest

from agent.mcp_backend import MCPBackend
from agent.retrieval import Retriever
from agent.tools import EvidenceStore, ToolExecutor
from pipelines.config import Settings
from pipelines.db import init_schema
from pipelines.fixtures import ingest as ingest_fixtures
from tests.test_tools import ACCESSION, TICKER, _cleanup, _seed

CASES: list[tuple[str, dict]] = [
    ("compute", {"expression": "(120-100)/100"}),
    ("xbrl_fact", {"ticker": TICKER, "tag_query": "revenue", "as_of": "2024-12-31"}),
    (
        "search_filings",
        {"query": "revenue", "tickers": [TICKER], "as_of": "2024-12-31"},
    ),
    ("get_section", {"doc_id": ACCESSION, "item": "7"}),
    ("compute", {"expression": "__import__('os')"}),  # tool-level bad_args
]


@pytest.fixture()
def seeded() -> None:
    url = Settings.from_env().database_url
    init_schema(url)
    ingest_fixtures(url)
    _cleanup(url)
    _seed(url)
    yield
    _cleanup(url)


@pytest.mark.asyncio
async def test_mcp_matches_in_process_observations(seeded: None) -> None:
    settings = Settings.from_env()
    retrieval = Retriever.from_settings(settings)
    backend = MCPBackend()
    try:
        for name, args in CASES:
            local_store = EvidenceStore()
            local = ToolExecutor(settings.database_url, local_store, retrieval=retrieval).execute(
                name, args
            )

            mcp_store = EvidenceStore()
            remote = await backend.execute(mcp_store, name, args)

            assert remote.ok == local.ok, f"{name}{args}: ok differs"
            assert remote.code == local.code, f"{name}{args}: code differs"
            assert remote.observation == local.to_observation(), (
                f"{name}{args}: observation differs\n"
                f"local={local.to_observation()[:400]}\nremote={remote.observation[:400]}"
            )
            assert list(mcp_store.handles) == list(local_store.handles), (
                f"{name}{args}: handle names differ"
            )
            assert list(mcp_store.facts) == list(local_store.facts), (
                f"{name}{args}: fact names differ"
            )
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_mcp_validation_errors_use_the_same_error_code(seeded: None) -> None:
    """MCP rejects missing args before the tool runs; the client must still say bad_args."""
    settings = Settings.from_env()
    local_store = EvidenceStore()
    local = ToolExecutor(
        settings.database_url, local_store, retrieval=Retriever.from_settings(settings)
    ).execute("xbrl_fact", {"ticker": TICKER})

    backend = MCPBackend()
    try:
        remote = await backend.execute(EvidenceStore(), "xbrl_fact", {"ticker": TICKER})
    finally:
        await backend.aclose()

    assert local.code == "bad_args"
    assert remote.code == "bad_args"
    # The protocol layer's message differs, but it still names what is missing.
    assert "as_of" in remote.observation
    assert "tag_query" in remote.observation
