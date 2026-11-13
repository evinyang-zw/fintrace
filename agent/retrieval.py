"""Retrieval tiers: BM25, dense, RRF fusion, optional rerank (W3).

One rule shapes the whole file: a tier that cannot run must *degrade visibly*.
Silently returning BM25 results when the caller asked for hybrid would make the
ablation table a lie, so every fallback lands in ``meta["degraded"]`` and in the
agent's trace.

At ~20k chunks the dense branch is an exact scan (no HNSW): with a filter on
company and filing date, an approximate index buys milliseconds and costs recall.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, cast

from agent.rerank import DashScopeReranker, Reranker
from pipelines.config import Settings
from pipelines.db import connect
from pipelines.embed import Embedder, build_embedder

RRF_K = 60
RERANK_POOL = 20
TIERS = {"bm25", "dense", "hybrid", "hybrid_rerank"}


@dataclass(frozen=True, slots=True)
class Hit:
    accession: str
    ticker: str
    form: str
    item_code: str
    char_start: int
    char_end: int
    text: str
    period_end: date
    filed_at: date
    score: float = 0.0
    source: str = "bm25"

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.accession, self.item_code, self.char_start)

    @property
    def citation_key(self) -> str:
        """Matches the ``TICKER/FORM/FY####`` prefix used by ``required_citations``."""
        return f"{self.ticker}/{self.form}/FY{self.period_end.year}#{self.item_code}"


@dataclass
class RetrievalMeta:
    requested_tier: str
    tier: str
    degraded: list[str] = field(default_factory=list)
    bm25_n: int = 0
    dense_n: int = 0
    fused_n: int = 0
    returned_n: int = 0
    rerank_used: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_tier": self.requested_tier,
            "tier": self.tier,
            "degraded": list(self.degraded),
            "bm25_n": self.bm25_n,
            "dense_n": self.dense_n,
            "fused_n": self.fused_n,
            "returned_n": self.returned_n,
            "rerank_used": self.rerank_used,
        }


_BM25_SQL = """
    SELECT ch.accession, ch.item_code, ch.char_start, ch.char_end, ch.text,
           f.form, f.filed_at, f.period_end, c.ticker,
           ts_rank_cd(
               to_tsvector('english', ch.text),
               websearch_to_tsquery('english', %(q)s)
           ) AS score
    FROM chunks ch
    JOIN filings f ON f.accession = ch.accession
    JOIN companies c ON c.cik = f.cik
    WHERE ch.filed_at <= %(as_of)s
      AND f.cik = ANY(%(ciks)s)
      AND (%(form)s::text IS NULL OR f.form = %(form)s)
      AND to_tsvector('english', ch.text) @@ websearch_to_tsquery('english', %(q)s)
    ORDER BY score DESC, ch.filed_at DESC, ch.accession, ch.item_code, ch.char_start
    LIMIT %(limit)s
"""

_DENSE_SQL = """
    SELECT ch.accession, ch.item_code, ch.char_start, ch.char_end, ch.text,
           f.form, f.filed_at, f.period_end, c.ticker,
           1 - (v.vec <=> %(vec)s::vector) AS score
    FROM chunk_vectors v
    JOIN chunks ch ON ch.id = v.chunk_id
    JOIN filings f ON f.accession = ch.accession
    JOIN companies c ON c.cik = f.cik
    WHERE v.model = %(model)s
      AND ch.filed_at <= %(as_of)s
      AND f.cik = ANY(%(ciks)s)
      AND (%(form)s::text IS NULL OR f.form = %(form)s)
    ORDER BY v.vec <=> %(vec)s::vector, ch.accession, ch.item_code, ch.char_start
    LIMIT %(limit)s
"""


def _row_to_hit(row: dict[str, Any], source: str) -> Hit:
    return Hit(
        accession=str(row["accession"]),
        ticker=str(row["ticker"]),
        form=str(row["form"]),
        item_code=str(row["item_code"]),
        char_start=int(cast(int, row["char_start"])),
        char_end=int(cast(int, row["char_end"])),
        text=str(row["text"]),
        period_end=cast(date, row["period_end"]),
        filed_at=cast(date, row["filed_at"]),
        score=float(cast(float, row["score"])),
        source=source,
    )


class Retriever:
    def __init__(
        self,
        database_url: str,
        *,
        tier: str = "bm25",
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        model: str = "",
    ) -> None:
        if tier not in TIERS:
            raise ValueError(f"unknown retrieval tier {tier!r}; choose from {sorted(TIERS)}")
        self._database_url = database_url
        self.tier = tier
        self._embedder = embedder
        self._reranker = reranker
        self._model = model or (embedder.model if embedder is not None else "")
        self._query_cache: dict[str, list[float]] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> Retriever:
        tier = settings.retrieval_tier
        embedder = None
        if settings.has_embeddings and tier in {"dense", "hybrid", "hybrid_rerank"}:
            embedder = build_embedder(settings)
        reranker: Reranker | None = None
        if tier == "hybrid_rerank" and settings.resolved_rerank_key:
            reranker = DashScopeReranker(
                url=settings.rerank_url,
                api_key=settings.resolved_rerank_key,
                model=settings.rerank_model,
            )
        return cls(
            settings.database_url,
            tier=tier,
            embedder=embedder,
            reranker=reranker,
            model=settings.embedding_model,
        )

    # -- public -----------------------------------------------------------

    def search(
        self,
        query: str,
        ciks: list[str],
        as_of: date,
        *,
        k: int = 8,
        form: str | None = None,
    ) -> tuple[list[Hit], RetrievalMeta]:
        meta = RetrievalMeta(requested_tier=self.tier, tier=self.tier)
        if not ciks:
            return [], meta

        wants_dense = self.tier in {"dense", "hybrid", "hybrid_rerank"}
        wants_lexical = self.tier in {"bm25", "hybrid", "hybrid_rerank"}
        pool = max(k, RERANK_POOL)

        bm25_hits: list[Hit] = []
        if wants_lexical:
            bm25_hits = self._bm25(query, ciks, as_of, limit=pool, form=form)
        meta.bm25_n = len(bm25_hits)

        dense_hits: list[Hit] = []
        if wants_dense:
            if self._embedder is None:
                meta.degraded.append("no_embedding_key")
            else:
                try:
                    dense_hits = self._dense(query, ciks, as_of, limit=pool, form=form)
                    meta.dense_n = len(dense_hits)
                except Exception as exc:  # provider/network/schema — never kill the run
                    meta.degraded.append(f"dense_failed:{type(exc).__name__}")

        if self.tier == "bm25":
            hits, effective = bm25_hits, "bm25"
        elif self.tier == "dense":
            if dense_hits:
                hits, effective = dense_hits, "dense"
            else:
                hits, effective = bm25_hits, "bm25"
                if not meta.degraded:
                    meta.degraded.append("no_vectors_for_model")
        else:
            hits, effective = self._combine(bm25_hits, dense_hits)
            meta.fused_n = len(hits)

        if self.tier == "hybrid_rerank" and self._reranker is not None and len(hits) > 1:
            hits, used = self._rerank(query, hits, k=k, meta=meta)
            meta.rerank_used = used
            if used:
                effective = "hybrid_rerank"

        meta.tier = effective
        result = hits[:k]
        meta.returned_n = len(result)
        return result, meta

    def _combine(self, bm25: list[Hit], dense: list[Hit]) -> tuple[list[Hit], str]:
        if bm25 and dense:
            return self._fuse(bm25, dense), "hybrid"
        if dense:
            return dense, "dense"
        return bm25, "bm25"

    # -- branches ---------------------------------------------------------

    def _bm25(
        self, query: str, ciks: list[str], as_of: date, *, limit: int, form: str | None
    ) -> list[Hit]:
        params = {
            "q": query,
            "as_of": as_of,
            "ciks": ciks,
            "form": form,
            "limit": limit,
        }
        with connect(self._database_url) as conn:
            rows = conn.execute(_BM25_SQL, params).fetchall()
        return [_row_to_hit(row, "bm25") for row in rows]

    def _dense(
        self, query: str, ciks: list[str], as_of: date, *, limit: int, form: str | None
    ) -> list[Hit]:
        assert self._embedder is not None
        vector = self._query_cache.get(query)
        if vector is None:
            vector = self._embedder.embed([query], kind="query")[0]
            self._query_cache[query] = vector
        params = {
            "vec": "[" + ",".join(f"{value:.8f}" for value in vector) + "]",
            "model": self._model,
            "as_of": as_of,
            "ciks": ciks,
            "form": form,
            "limit": limit,
        }
        with connect(self._database_url) as conn:
            rows = conn.execute(_DENSE_SQL, params).fetchall()
        return [_row_to_hit(row, "dense") for row in rows]

    def _fuse(self, bm25: list[Hit], dense: list[Hit]) -> list[Hit]:
        scores: dict[tuple[str, str, int], float] = {}
        by_key: dict[tuple[str, str, int], Hit] = {}
        for ranked in (bm25, dense):
            for rank, hit in enumerate(ranked, start=1):
                scores[hit.key] = scores.get(hit.key, 0.0) + 1.0 / (RRF_K + rank)
                by_key.setdefault(hit.key, hit)
        ordered = sorted(
            scores.items(),
            key=lambda pair: (-pair[1], pair[0][0], pair[0][1], pair[0][2]),
        )
        fused: list[Hit] = []
        for key, score in ordered:
            base = by_key[key]
            fused.append(
                Hit(
                    accession=base.accession,
                    ticker=base.ticker,
                    form=base.form,
                    item_code=base.item_code,
                    char_start=base.char_start,
                    char_end=base.char_end,
                    text=base.text,
                    period_end=base.period_end,
                    filed_at=base.filed_at,
                    score=score,
                    source="rrf",
                )
            )
        return fused

    def _rerank(
        self, query: str, hits: list[Hit], *, k: int, meta: RetrievalMeta
    ) -> tuple[list[Hit], bool]:
        assert self._reranker is not None
        # Reorder the fused top-k, do not select from a larger pool: W3 showed
        # that 20 -> 8 selection drops the required section for ~13% of F2
        # questions. Rerank should change order, not recall.
        pool = hits[:k]
        try:
            ranked = self._reranker.rerank(
                query, [hit.text[:1200] for hit in pool], top_n=min(k, len(pool))
            )
        except Exception as exc:
            meta.degraded.append(f"rerank_failed:{type(exc).__name__}")
            meta.tier = "hybrid"
            return hits, False
        reordered = [pool[index] for index, _score in ranked]
        # Anything the reranker did not return is appended in fused order, so a
        # short rerank response cannot silently shrink the context.
        seen = {hit.key for hit in reordered}
        reordered.extend(hit for hit in hits if hit.key not in seen)
        return reordered, True


def citation_key(ticker: str, form: str, period_end: date, item: str) -> str:
    return f"{ticker}/{form}/FY{period_end.year}#{item}"
