"""Embedding response validation: dimension errors must stop the backfill."""

from __future__ import annotations

import pytest

from pipelines.embed import EmbeddingError, OpenAICompatEmbedder


def embedder(dim: int = 4) -> OpenAICompatEmbedder:
    return OpenAICompatEmbedder(base_url="http://unused", api_key="k", model="m", dim=dim)


def test_parse_orders_by_index() -> None:
    payload = {
        "data": [
            {"index": 1, "embedding": [0.4, 0.5, 0.6, 0.7]},
            {"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]},
        ]
    }
    rows = embedder()._parse(payload, expected=2)
    assert rows[0][0] == pytest.approx(0.1)
    assert rows[1][0] == pytest.approx(0.4)


def test_dimension_mismatch_is_fatal() -> None:
    payload = {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}
    with pytest.raises(EmbeddingError, match="dimension mismatch"):
        embedder(dim=4)._parse(payload, expected=1)


def test_missing_row_is_fatal() -> None:
    payload = {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]}
    with pytest.raises(EmbeddingError):
        embedder()._parse(payload, expected=2)
