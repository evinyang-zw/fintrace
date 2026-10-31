"""Cross-encoder rerank over the fused candidate list (W3).

DashScope's rerank endpoint is *not* the OpenAI chat shape, so it gets its own
twenty lines instead of an SDK. The caller must treat failure as "keep the
pre-rerank order and say so" — a rerank timeout should degrade quality, not take
the whole agent down.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx

RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504})
DEFAULT_TIMEOUT_S = 20.0


class RerankError(RuntimeError):
    """Non-retryable rerank failure."""


class Reranker(Protocol):
    name: str

    def rerank(
        self, query: str, documents: Sequence[str], *, top_n: int
    ) -> list[tuple[int, float]]:
        """Return ``(index, score)`` for the best ``top_n`` documents."""
        ...


@dataclass(slots=True)
class DashScopeReranker:
    url: str
    api_key: str
    model: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    attempts: int = 2

    name = "dashscope-rerank"

    def __post_init__(self) -> None:
        if not self.api_key:
            raise RerankError("rerank api key is empty")

    def rerank(
        self, query: str, documents: Sequence[str], *, top_n: int
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        payload: dict[str, Any] = {
            "model": self.model,
            "input": {"query": query, "documents": list(documents)},
            "parameters": {"top_n": min(top_n, len(documents)), "return_documents": False},
        }
        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    response = client.post(
                        self.url,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
            except httpx.TransportError as exc:
                last = exc
                time.sleep(0.4 * attempt)
                continue
            if response.status_code in RETRYABLE and attempt < self.attempts:
                last = RerankError(f"HTTP {response.status_code}")
                time.sleep(0.4 * attempt)
                continue
            if response.status_code >= 400:
                raise RerankError(f"HTTP {response.status_code} from rerank: {response.text[:300]}")
            return self._parse(response.json(), limit=len(documents))
        raise RerankError(f"rerank failed after retries: {last}")

    def _parse(self, payload: dict[str, Any], *, limit: int) -> list[tuple[int, float]]:
        output = payload.get("output")
        if not isinstance(output, dict):
            raise RerankError("rerank response has no output object")
        results = cast(dict[str, Any], output).get("results")
        if not isinstance(results, list):
            raise RerankError("rerank response has no results list")
        ranked: list[tuple[int, float]] = []
        for item in results:
            raw = cast(dict[str, Any], item)
            index = int(raw.get("index", -1))
            score = float(raw.get("relevance_score", 0.0))
            if 0 <= index < limit:
                ranked.append((index, score))
        if not ranked:
            raise RerankError("rerank returned no usable results")
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        return ranked


class NoopReranker:
    """Used when rerank is disabled or unavailable; keeps the input order."""

    name = "noop"

    def rerank(
        self, query: str, documents: Sequence[str], *, top_n: int
    ) -> list[tuple[int, float]]:
        return [(index, 0.0) for index in range(min(top_n, len(documents)))]
