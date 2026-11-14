"""The MCP payload must carry retrieval bookkeeping, not just text.

W5 audit found Answer.retrieval empty in production: the server recorded
search_calls in its own EvidenceStore and never sent them back, so tier
dashboards and recall had nothing to read.
"""

from __future__ import annotations

from agent.mcp_backend import _payload_to_result
from agent.tools import EvidenceStore

OBSERVATION = '{"results": [{"handle": "K2"}], "retrieval": {"tier": "hybrid"}}'


def test_payload_restores_handles_search_calls_and_usage() -> None:
    store = EvidenceStore()
    payload = {
        "ok": True,
        "code": None,
        "observation": OBSERVATION,
        "handles": [
            {
                "name": "K2",
                "accession": "AAPL/10-K/FY2024",
                "item_code": "7",
                "char_start": 0,
                "char_end": 10,
                "quote": "391,035",
                "label": "AAPL/10-K/2024-09-28",
                "filed_at": "2024-11-01",
            }
        ],
        "facts": [],
        "search_calls": [
            {"query": "revenue", "tier": "hybrid", "degraded": [], "hits": ["AAPL/10-K/FY2024#7"]}
        ],
        "usage": {"embedding_tokens": 12, "rerank_tokens": 0},
    }
    result = _payload_to_result(payload, store)
    assert result.ok is True
    assert store.search_calls == payload["search_calls"]
    assert store.embedding_tokens == 12
    assert store.rerank_tokens == 0
    # The server's K2 becomes the client's K1; the observation is remapped.
    assert '"handle": "K1"' in result.observation
    assert "K2" not in result.observation


def test_payload_without_new_fields_still_works() -> None:
    store = EvidenceStore()
    result = _payload_to_result({"ok": False, "code": "not_found", "observation": "x"}, store)
    assert result.ok is False
    assert store.search_calls == []
    assert store.embedding_tokens == 0
