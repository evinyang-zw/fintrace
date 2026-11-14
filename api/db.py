"""Postgres access for the service's ``runs`` table.

The graph checkpointer already owns conversation state; this table owns the
*product* state: what a user asked, what it cost, and whether a human still has
to look at it. Two records, two jobs — do not merge them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

TERMINAL = {"done", "failed", "cancelled"}


def _json(value: Any) -> Jsonb:
    return Jsonb(json.loads(json.dumps(value, default=str)))


class RunStore:
    def __init__(self, database_url: str) -> None:
        self._pool = AsyncConnectionPool(
            database_url,
            min_size=1,
            max_size=8,
            open=False,
            kwargs={"row_factory": dict_row},
        )

    async def open(self) -> None:
        await self._pool.open()
        await self._pool.wait()
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    async def close(self) -> None:
        await self._pool.close()

    async def ping(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("SELECT 1")

    async def create(
        self,
        run_id: UUID,
        thread_id: str,
        question: dict[str, Any],
        *,
        review: bool,
        client_ip: str,
        token_kind: str,
    ) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    """
                    INSERT INTO runs (id, thread_id, question, status, review, client_ip, token_kind)
                    VALUES (%s, %s, %s, 'queued', %s, %s, %s)
                    RETURNING *
                    """,
                    (run_id, thread_id, _json(question), review, client_ip, token_kind),
                )
            ).fetchone()
        assert row is not None
        return cast(dict[str, Any], row)

    async def get(self, run_id: UUID) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT * FROM runs WHERE id = %s", (run_id,))
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    async def list_pending(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT * FROM runs WHERE status = 'awaiting_review'"
                    " ORDER BY queued_at DESC LIMIT %s",
                    (limit,),
                )
            ).fetchall()
        return list(cast(list[dict[str, Any]], rows))

    async def mark_running(self, run_id: UUID) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET status = 'running', started_at = now()"
                " WHERE id = %s AND status = 'queued'",
                (run_id,),
            )

    async def finish(self, run_id: UUID, answer: dict[str, Any], usage: dict[str, Any]) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET status = 'done', answer = %s, usage = %s,"
                " finished_at = now(), error = NULL WHERE id = %s",
                (_json(answer), _json(usage), run_id),
            )

    async def await_review(self, run_id: UUID, thread_id: str, payload: dict[str, Any]) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET status = 'awaiting_review', thread_id = %s,"
                " review_payload = %s WHERE id = %s",
                (thread_id, _json(payload), run_id),
            )

    async def fail(self, run_id: UUID, error: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET status = 'failed', error = %s, finished_at = now()"
                " WHERE id = %s AND status <> 'done'",
                (error[:2000], run_id),
            )

    async def reap_stale(self, older_than_s: int = 900) -> int:
        """A process restart must not leave rows 'running' forever."""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE runs SET status = 'failed',"
                " error = 'interrupted by restart', finished_at = now()"
                " WHERE status = 'running'"
                " AND started_at < now() - make_interval(secs => %s)",
                (older_than_s,),
            )
            return cursor.rowcount or 0

    async def today_spend(self) -> float:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT coalesce(sum((usage->>'cost_usd')::float8), 0) AS spent"
                    " FROM runs WHERE status = 'done' AND finished_at >= date_trunc('day', now())"
                )
            ).fetchone()
        return float(cast(dict[str, Any], row)["spent"]) if row else 0.0

    async def ip_usage_today(self, ip: str) -> int:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM runs WHERE client_ip = %s"
                    " AND queued_at >= date_trunc('day', now())",
                    (ip,),
                )
            ).fetchone()
        return int(cast(dict[str, Any], row)["n"]) if row else 0

    async def stats(self, days: int = 7) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT
                      count(*) AS total,
                      count(*) FILTER (WHERE status = 'done') AS done,
                      count(*) FILTER (WHERE status = 'failed') AS failed,
                      count(*) FILTER (WHERE status = 'awaiting_review') AS pending_review,
                      coalesce(avg((usage->>'cost_usd')::float8)
                               FILTER (WHERE status = 'done'), 0) AS avg_cost,
                      coalesce(sum((usage->>'cost_usd')::float8)
                               FILTER (WHERE status = 'done'), 0) AS total_cost,
                      percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY extract(epoch FROM (finished_at - started_at)) * 1000
                      ) FILTER (WHERE status = 'done') AS p50_ms,
                      percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY extract(epoch FROM (finished_at - started_at)) * 1000
                      ) FILTER (WHERE status = 'done') AS p95_ms,
                      coalesce(sum((usage->>'tokens_in')::bigint)
                               FILTER (WHERE status = 'done'), 0) AS tokens_in,
                      coalesce(sum((usage->>'tokens_cached')::bigint)
                               FILTER (WHERE status = 'done'), 0) AS tokens_cached,
                      coalesce(sum((usage->>'tokens_out')::bigint)
                               FILTER (WHERE status = 'done'), 0) AS tokens_out
                    FROM runs
                    WHERE queued_at > now() - make_interval(days => %s)
                    """,
                    (days,),
                )
            ).fetchone()
        data = dict(cast(dict[str, Any], row or {}))
        total = int(data.get("total") or 0)
        done = int(data.get("done") or 0)
        data["success_rate"] = (done / total) if total else 0.0
        data["cost_per_question"] = float(data.get("avg_cost") or 0.0)
        data["total_cost"] = float(data.get("total_cost") or 0.0)
        data["p50_ms"] = float(data.get("p50_ms") or 0.0)
        data["p95_ms"] = float(data.get("p95_ms") or 0.0)
        data["cache_hit_rate"] = (
            float(data["tokens_cached"]) / float(data["tokens_in"])
            if data.get("tokens_in")
            else 0.0
        )
        data["days"] = days
        data["daily"] = await self.daily_stats(days)
        data["by_tier"] = await self.tier_stats(days)
        data["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return data

    async def tier_stats(self, days: int) -> list[dict[str, Any]]:
        """Cost/latency per retrieval tier, one row per run (not per search call)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT tier,
                           count(*) AS n,
                           coalesce(avg(cost), 0) AS avg_cost,
                           percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
                    FROM (
                        SELECT DISTINCT runs.id,
                               item->>'tier' AS tier,
                               (runs.usage->>'cost_usd')::float8 AS cost,
                               extract(epoch FROM (runs.finished_at - runs.started_at)) * 1000
                                   AS latency_ms
                        FROM runs
                        CROSS JOIN LATERAL jsonb_array_elements(
                            coalesce(runs.answer->'retrieval', '[]'::jsonb)
                        ) AS item
                        WHERE runs.status = 'done'
                          AND runs.queued_at > now() - make_interval(days => %s)
                    ) per_run
                    GROUP BY tier ORDER BY n DESC
                    """,
                    (days,),
                )
            ).fetchall()
        return [
            {
                "tier": str(row["tier"]),
                "n": int(row["n"]),
                "avg_cost": float(row["avg_cost"] or 0.0),
                "p95_ms": float(row["p95_ms"] or 0.0),
            }
            for row in cast(list[dict[str, Any]], rows)
        ]

    async def daily_stats(self, days: int) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT date_trunc('day', queued_at)::date AS day,
                           count(*) AS total,
                           count(*) FILTER (WHERE status = 'done') AS done,
                           count(*) FILTER (WHERE status = 'failed') AS failed,
                           coalesce(sum((usage->>'cost_usd')::float8)
                                    FILTER (WHERE status = 'done'), 0) AS cost,
                           percentile_cont(0.95) WITHIN GROUP (
                             ORDER BY extract(epoch FROM (finished_at - started_at)) * 1000
                           ) FILTER (WHERE status = 'done') AS p95_ms
                    FROM runs
                    WHERE queued_at > now() - make_interval(days => %s)
                    GROUP BY 1 ORDER BY 1 DESC
                    """,
                    (days,),
                )
            ).fetchall()
        return [
            {
                "day": row["day"].isoformat(),
                "total": int(row["total"]),
                "done": int(row["done"]),
                "failed": int(row["failed"]),
                "cost": float(row["cost"]),
                "p95_ms": float(row["p95_ms"] or 0.0),
            }
            for row in cast(list[dict[str, Any]], rows)
        ]
