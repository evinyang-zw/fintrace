"""Find runs paused at the human-review node.

W4 kept a ``review_queue`` table for this; W5 made ``runs`` the single source of
truth (``status = 'awaiting_review'``). Keeping both tables meant two places to
look when a pending run "disappeared", so the table is no longer written and
this module only reads the run store.
"""

from __future__ import annotations

from typing import Any

from pipelines.db import connect


def list_pending(database_url: str) -> list[dict[str, Any]]:
    with connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT thread_id,
                   coalesce(question->>'id', '') AS question_id,
                   coalesce(review_payload->'draft'->>'text', '') AS draft,
                   coalesce(review_payload->'verification', '{}'::jsonb) AS verification,
                   queued_at AS created_at
            FROM runs
            WHERE status = 'awaiting_review'
            ORDER BY queued_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
