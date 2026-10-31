"""Embed the corpus into ``chunk_vectors`` (W3), once per model.

Design notes:

* the model name is part of the row; switching providers never mixes two vector
  spaces inside one query (the failure mode is silent and ugly);
* backfill is resumable: it only asks for chunks that have no row for this
  model, and ``ON CONFLICT DO NOTHING`` makes a re-run free;
* ``--smoke`` exists because the expensive mistake is paying for 20k embeddings
  from a provider whose dimensions or auth were wrong. Fifteen seconds first.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
import typer
from rich.console import Console

from pipelines.config import Settings
from pipelines.db import connect, init_schema

DEFAULT_BATCH = 10  # DashScope rejects batches larger than 10
RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504})


class EmbeddingError(RuntimeError):
    """A failure that should stop the backfill instead of being retried forever."""


EmbedKind = str  # "document" | "query"


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: Sequence[str], *, kind: EmbedKind = "document") -> list[list[float]]: ...


@dataclass(slots=True)
class OpenAICompatEmbedder:
    """``POST {base}/embeddings``; works for DashScope's compatible mode."""

    base_url: str
    api_key: str
    model: str
    dim: int
    timeout_s: float = 30.0
    attempts: int = 3

    def __post_init__(self) -> None:
        if not self.api_key:
            raise EmbeddingError(
                "DASHSCOPE_API_KEY is empty. Add it to .env (see .env.example) "
                "or run with RETRIEVAL_TIER=bm25."
            )

    def embed(self, texts: Sequence[str], *, kind: EmbedKind = "document") -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {
            "model": self.model,
            "input": list(texts),
            "dimensions": self.dim,
            "encoding_format": "float",
        }
        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    response = client.post(
                        f"{self.base_url.rstrip('/')}/embeddings",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
            except httpx.TransportError as exc:
                last = exc
                time.sleep(0.5 * 2**attempt)
                continue
            if response.status_code in RETRYABLE and attempt < self.attempts:
                last = EmbeddingError(f"HTTP {response.status_code}")
                time.sleep(0.5 * 2**attempt)
                continue
            if response.status_code >= 400:
                raise EmbeddingError(
                    f"HTTP {response.status_code} from embeddings endpoint: {response.text[:300]}"
                )
            return self._parse(response.json(), expected=len(texts))
        raise EmbeddingError(f"embedding request failed after retries: {last}")

    def _parse(self, payload: dict[str, Any], *, expected: int) -> list[list[float]]:
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != expected:
            raise EmbeddingError(f"expected {expected} embeddings, got {type(data).__name__}")
        rows: list[list[float] | None] = [None] * expected
        for item in data:
            raw = cast(dict[str, Any], item)
            index = int(raw.get("index", 0))
            vector = raw.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError(f"embedding {index} is empty")
            floats = [float(value) for value in vector]
            if len(floats) != self.dim:
                raise EmbeddingError(
                    f"dimension mismatch: model returned {len(floats)}, configured {self.dim}. "
                    "Fix EMBEDDING_DIM before writing any vectors."
                )
            if index >= expected:
                raise EmbeddingError(f"embedding index {index} out of range {expected}")
            rows[index] = floats
        if any(row is None for row in rows):
            raise EmbeddingError("response is missing one or more embeddings")
        return [row for row in rows if row is not None]


@dataclass(slots=True)
class DashScopeEmbedder:
    """DashScope native embeddings API (``/api/v1/services/embeddings/...``).

    Bailian workspace endpoints are native, not OpenAI-compatible: the payload
    is ``input.texts`` and the answer comes back under ``output.embeddings``.
    ``text_type`` lets the same model embed documents and queries asymmetrically.
    """

    url: str
    api_key: str
    model: str
    dim: int
    timeout_s: float = 30.0
    attempts: int = 3

    def __post_init__(self) -> None:
        if not self.api_key:
            raise EmbeddingError(
                "DASHSCOPE_API_KEY is empty. Add it to .env or run with RETRIEVAL_TIER=bm25."
            )

    def embed(self, texts: Sequence[str], *, kind: EmbedKind = "document") -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {
            "model": self.model,
            "input": {"texts": list(texts)},
            "parameters": {"dimension": self.dim, "text_type": kind},
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
                time.sleep(0.5 * 2**attempt)
                continue
            if response.status_code in RETRYABLE and attempt < self.attempts:
                last = EmbeddingError(f"HTTP {response.status_code}")
                time.sleep(0.5 * 2**attempt)
                continue
            if response.status_code >= 400:
                raise EmbeddingError(
                    f"HTTP {response.status_code} from embeddings endpoint: {response.text[:300]}"
                )
            return self._parse(response.json(), expected=len(texts))
        raise EmbeddingError(f"embedding request failed after retries: {last}")

    def _parse(self, payload: dict[str, Any], *, expected: int) -> list[list[float]]:
        output = payload.get("output")
        if not isinstance(output, dict):
            raise EmbeddingError("response has no output object")
        raw = cast(dict[str, Any], output).get("embeddings")
        if not isinstance(raw, list) or len(raw) != expected:
            raise EmbeddingError(f"expected {expected} embeddings, got {type(raw).__name__}")
        rows: list[list[float] | None] = [None] * expected
        for position, item in enumerate(raw):
            vector: Any
            if isinstance(item, list):
                index, vector = position, item
            elif isinstance(item, dict):
                entry = cast(dict[str, Any], item)
                index = int(entry.get("text_index", position))
                vector = entry.get("embedding")
            else:
                raise EmbeddingError("malformed embedding item")
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError(f"embedding {index} is empty")
            floats = [float(value) for value in vector]
            if len(floats) != self.dim:
                raise EmbeddingError(
                    f"dimension mismatch: model returned {len(floats)}, configured {self.dim}. "
                    "Fix EMBEDDING_DIM before writing any vectors."
                )
            if not 0 <= index < expected:
                raise EmbeddingError(f"embedding index {index} out of range {expected}")
            rows[index] = floats
        if any(row is None for row in rows):
            raise EmbeddingError("response is missing one or more embeddings")
        return [row for row in rows if row is not None]


def build_embedder(settings: Settings) -> Embedder:
    """Pick the wire format from the URL instead of making the user configure it."""
    if "/api/v1/services/" in settings.embedding_base_url:
        return DashScopeEmbedder(
            url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
        )
    return OpenAICompatEmbedder(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
    )


def _to_pgvector(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"


def backfill(
    settings: Settings,
    *,
    batch: int = DEFAULT_BATCH,
    limit: int | None = None,
    force: bool = False,
    embedder: Embedder | None = None,
    workers: int = 4,
) -> int:
    """Embed pending chunks with a small in-flight window.

    One request at a time is ~1.5 s per 10 chunks, i.e. roughly an hour for the
    corpus; four concurrent requests cut that to minutes. HTTP happens in worker
    threads, every database write happens on the main connection, and each batch
    commits immediately so a crash resumes instead of restarting.
    """
    if embedder is None:
        embedder = build_embedder(settings)
    init_schema(settings.database_url)
    if force:
        with connect(settings.database_url) as conn:
            conn.execute("DELETE FROM chunk_vectors WHERE model = %s", (settings.embedding_model,))
            conn.commit()

    done = 0
    pending: dict[Future[list[list[float]]], list[dict[str, Any]]] = {}
    in_flight_ids: set[int] = set()
    exhausted = False

    with connect(settings.database_url) as conn, ThreadPoolExecutor(max_workers=workers) as pool:

        def fetch(take: int, skip: set[int]) -> list[dict[str, Any]]:
            # Rows already submitted to a worker are still invisible to the
            # vector join (they are not inserted yet), so they must be excluded
            # explicitly — otherwise every window re-embeds the same chunks.
            return conn.execute(
                """
                SELECT c.id, c.text
                FROM chunks c
                LEFT JOIN chunk_vectors v
                  ON v.chunk_id = c.id AND v.model = %s
                WHERE v.chunk_id IS NULL
                  AND NOT (c.id = ANY(%s))
                ORDER BY c.id
                LIMIT %s
                """,
                (settings.embedding_model, list(skip), take),
            ).fetchall()

        while pending or not exhausted:
            while not exhausted and len(pending) < workers:
                in_flight = sum(len(rows) for rows in pending.values())
                remaining = None if limit is None else limit - done - in_flight
                if remaining is not None and remaining <= 0:
                    exhausted = True
                    break
                take = batch if remaining is None else min(batch, remaining)
                rows = fetch(take, in_flight_ids)
                if not rows:
                    exhausted = True
                    break
                texts = [str(row["text"]) for row in rows]
                in_flight_ids |= {int(cast(int, row["id"])) for row in rows}
                pending[pool.submit(embedder.embed, texts)] = rows

            if not pending:
                break
            finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for future in finished:
                rows = pending.pop(future)
                in_flight_ids -= {int(cast(int, row["id"])) for row in rows}
                vectors = future.result()
                for row, vector in zip(rows, vectors, strict=True):
                    conn.execute(
                        "INSERT INTO chunk_vectors (chunk_id, model, dim, vec) "
                        "VALUES (%s, %s, %s, %s::vector) ON CONFLICT DO NOTHING",
                        (
                            int(cast(int, row["id"])),
                            settings.embedding_model,
                            settings.embedding_dim,
                            _to_pgvector(vector),
                        ),
                    )
                done += len(rows)
            conn.commit()
            if done % 500 == 0:
                print(f"  {done} chunks embedded", flush=True)
    return done


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


SMOKE_TEXTS = [
    "Apple total net revenue fiscal 2024",
    "苹果公司 2024 财年总营收",
    "The company repurchased shares under its buyback program",
]


def smoke(settings: Settings) -> None:
    """D1 gate: dimension, non-zero, and a sanity ordering on three texts."""
    console = Console()
    embedder = build_embedder(settings)
    started = time.perf_counter()
    vectors = embedder.embed(SMOKE_TEXTS)
    elapsed = (time.perf_counter() - started) * 1000
    console.print(
        f"model={embedder.model} dim={len(vectors[0])} latency={elapsed:.0f}ms "
        f"(requested {settings.embedding_dim})"
    )
    if any(all(value == 0 for value in vector) for vector in vectors):
        raise EmbeddingError("model returned an all-zero vector")
    same = _cosine(vectors[0], vectors[1])
    other = _cosine(vectors[0], vectors[2])
    console.print(f"cos(en, zh)={same:.3f}  cos(en, unrelated)={other:.3f}")
    if same <= other:
        raise EmbeddingError(
            "cross-lingual pair is not closer than the unrelated pair; "
            "wrong model or not multilingual"
        )
    console.print("[green]smoke passed[/green]")


app = typer.Typer(add_completion=False)


@app.command()
def main(
    batch: int = typer.Option(DEFAULT_BATCH, help="每批文本数"),
    limit: int | None = typer.Option(None, help="本次最多嵌入多少个 chunk"),
    force: bool = typer.Option(False, help="先删除该模型的旧向量再重算"),
    workers: int = typer.Option(4, help="并发请求数"),
    smoke_only: bool = typer.Option(False, "--smoke", help="只做连通性验证，不写库"),
) -> None:
    settings = Settings.from_env()
    console = Console()
    if smoke_only:
        smoke(settings)
        return
    if not settings.has_embeddings:
        raise typer.BadParameter(
            "DASHSCOPE_API_KEY is empty; add it to .env or use --smoke after setting it"
        )
    total = backfill(settings, batch=batch, limit=limit, force=force, workers=workers)
    console.print(f"[green]✓[/green] embedded {total} chunks with {settings.embedding_model}")


if __name__ == "__main__":
    app()
