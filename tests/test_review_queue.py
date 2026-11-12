"""The review queue is a convenience table; it must not leak rows."""

from __future__ import annotations

import uuid

import pytest

from agent.review_queue import dequeue, enqueue, list_pending
from pipelines.config import Settings
from pipelines.db import init_schema


@pytest.fixture()
def database_url() -> str:
    url = Settings.from_env().database_url
    init_schema(url)
    return url


def test_enqueue_list_dequeue_roundtrip(database_url: str) -> None:
    thread_id = f"test-{uuid.uuid4().hex[:8]}"
    try:
        enqueue(
            database_url,
            thread_id=thread_id,
            question_id="q-1",
            draft="草稿",
            verification={"passed": False, "problems": ["x"]},
        )
        rows = {row["thread_id"]: row for row in list_pending(database_url)}
        assert thread_id in rows
        assert rows[thread_id]["question_id"] == "q-1"
        assert rows[thread_id]["verification"]["passed"] is False

        dequeue(database_url, thread_id)
        assert thread_id not in {row["thread_id"] for row in list_pending(database_url)}
    finally:
        dequeue(database_url, thread_id)


def test_enqueue_is_idempotent(database_url: str) -> None:
    thread_id = f"test-{uuid.uuid4().hex[:8]}"
    try:
        enqueue(
            database_url,
            thread_id=thread_id,
            question_id="q-2",
            draft="v1",
            verification=None,
        )
        enqueue(
            database_url,
            thread_id=thread_id,
            question_id="q-2",
            draft="v2",
            verification=None,
        )
        rows = [row for row in list_pending(database_url) if row["thread_id"] == thread_id]
        assert len(rows) == 1
        assert rows[0]["draft"] == "v2"
    finally:
        dequeue(database_url, thread_id)
