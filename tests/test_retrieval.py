"""Retrieval tiers: fusion order, degradation visibility, and citation keys."""

from __future__ import annotations

from datetime import date

import pytest

from agent.retrieval import Hit, RetrievalMeta, Retriever, citation_key

AS_OF = date(2025, 1, 1)
CIKS = ["0000000001"]


def hit(name: str, *, item: str = "7", ticker: str = "TST") -> Hit:
    return Hit(
        accession=f"doc-{name}",
        ticker=ticker,
        form="10-K",
        item_code=item,
        char_start=0,
        char_end=10,
        text=f"text {name}",
        period_end=date(2024, 12, 31),
        filed_at=date(2024, 11, 1),
    )


class _Embedder:
    model = "fake-embed"
    dim = 4

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _BoomReranker:
    name = "boom"

    def rerank(self, query: str, documents: list[str], *, top_n: int):
        raise RuntimeError("provider down")


def test_rrf_puts_agreement_first() -> None:
    retriever = Retriever("postgresql://unused", tier="hybrid")
    a, b, c = hit("a"), hit("b"), hit("c")
    retriever._bm25 = lambda *args, **kwargs: [a, b]  # type: ignore[method-assign]
    retriever._dense = lambda *args, **kwargs: [b, c]  # type: ignore[method-assign]
    retriever._embedder = _Embedder()  # type: ignore[assignment]

    hits, meta = retriever.search("q", CIKS, AS_OF, k=3)

    assert meta.tier == "hybrid"
    assert next(h.accession for h in hits) == b.accession  # found by both branches


def test_hybrid_without_key_degrades_loudly() -> None:
    retriever = Retriever("postgresql://unused", tier="hybrid")
    retriever._bm25 = lambda *args, **kwargs: [hit("a")]  # type: ignore[method-assign]

    hits, meta = retriever.search("q", CIKS, AS_OF, k=3)

    assert len(hits) == 1
    assert meta.tier == "bm25"
    assert "no_embedding_key" in meta.degraded


def test_rerank_failure_keeps_fused_order_and_says_so() -> None:
    retriever = Retriever("postgresql://unused", tier="hybrid_rerank")
    retriever._bm25 = lambda *args, **kwargs: [hit("a"), hit("b")]  # type: ignore[method-assign]
    retriever._embedder = _Embedder()  # type: ignore[assignment]
    retriever._dense = lambda *args, **kwargs: [hit("b"), hit("c")]  # type: ignore[method-assign]
    retriever._reranker = _BoomReranker()  # type: ignore[assignment]

    hits, meta = retriever.search("q", CIKS, AS_OF, k=3)

    assert meta.rerank_used is False
    assert any(item.startswith("rerank_failed") for item in meta.degraded)
    assert {h.accession for h in hits} == {"doc-a", "doc-b", "doc-c"}


def test_unknown_tier_is_rejected_early() -> None:
    with pytest.raises(ValueError, match="unknown retrieval tier"):
        Retriever("postgresql://unused", tier="magic")


def test_citation_key_matches_required_citation_format() -> None:
    assert citation_key("MSFT", "10-K", date(2024, 6, 30), "7") == "MSFT/10-K/FY2024#7"


def test_meta_serialises_without_private_state() -> None:
    meta = RetrievalMeta(requested_tier="hybrid", tier="bm25", degraded=["no_embedding_key"])
    payload = meta.as_dict()
    assert payload["tier"] == "bm25"
    assert payload["degraded"] == ["no_embedding_key"]
