"""Tiny queue for runs paused at the human-review node.

LangGraph persists the run itself; this table only exists so a UI can *find*
pending runs without knowing every thread id in advance. It is written right
before ``interrupt()`` and removed in ``finalize()``.
"""

from __future__ import annotations

import json
from typing import Any

from pipelines.db import connect


def enqueue(
    database_url: str,
    *,
    thread_id: str,
    question_id: str,
    draft: str,
    verification: dict[str, Any] | None,
) -> None:
    with connect(database_url) as conn:
        conn.execute(
            """
            INSERT INTO review_queue (thread_id, question_id, draft, verification)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (thread_id) DO UPDATE
                SET draft = EXCLUDED.draft, verification = EXCLUDED.verification
            """,
            (thread_id, question_id, draft, json.dumps(verification or {}, ensure_ascii=False)),
        )


def dequeue(database_url: str, thread_id: str) -> None:
    with connect(database_url) as conn:
        conn.execute("DELETE FROM review_queue WHERE thread_id = %s", (thread_id,))


def list_pending(database_url: str) -> list[dict[str, Any]]:
    with connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT thread_id, question_id, draft, verification, created_at
            FROM review_queue ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
