"""Database connection + schema bootstrap."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@contextmanager
def connect(database_url: str) -> Iterator[Connection[dict[str, object]]]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        yield conn


def init_schema(database_url: str) -> None:
    """Apply the idempotent DDL. Safe to call on every run."""
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(database_url) as conn:
        conn.execute(ddl)
