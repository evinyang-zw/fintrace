"""Integration tests against the real Postgres schema.

Skipped automatically when no database is reachable; in CI the pgvector service
is always up, so these run there. They seed a throwaway company and remove it
afterwards, so they are safe against a warehouse that already has data.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import psycopg
import pytest

from eval.check import _check_citations, _check_ledger
from eval.schema import Gold, GoldSource, Question
from pipelines.config import Settings
from pipelines.db import connect, init_schema

CIK = "9999999999"
TICKER = "ZZTEST"
ACCESSION = "TEST-0001"
TAG = "TestRevenue"


@pytest.fixture()
def database_url() -> Iterator[str]:
    url = Settings.from_env().database_url
    try:
        psycopg.connect(url, connect_timeout=2).close()
    except Exception as exc:
        pytest.skip(f"postgres unavailable: {exc}")

    init_schema(url)
    _cleanup(url)
    with connect(url) as conn:
        conn.execute(
            "INSERT INTO companies (cik, ticker, name) VALUES (%s, %s, %s)",
            (CIK, TICKER, "Test Company"),
        )
        conn.execute(
            """
            INSERT INTO filings (accession, cik, form, filed_at, period_end)
            VALUES (%s, %s, '10-K', %s, %s)
            """,
            (ACCESSION, CIK, date(2024, 11, 1), date(2024, 9, 28)),
        )
        conn.execute(
            """
            INSERT INTO sections (accession, item_code, heading, char_start, char_end, text)
            VALUES (%s, '8', 'Item 8', 0, 20, 'revenue was 100 million')
            """,
            (ACCESSION,),
        )
        conn.execute(
            """
            INSERT INTO chunks (accession, filed_at, item_code, char_start, char_end, text, n_tokens)
            VALUES (%s, %s, '8', 0, 20, 'revenue was 100 million', 5)
            """,
            (ACCESSION, date(2024, 11, 1)),
        )
        conn.execute(
            """
            INSERT INTO facts (cik, tag, unit, period_start, period_end, value, filed_at, form, accn)
            VALUES (%s, %s, 'USD', %s, %s, 100, %s, '10-K', %s)
            """,
            (CIK, TAG, date(2023, 10, 1), date(2024, 9, 28), date(2024, 11, 1), ACCESSION),
        )

    yield url
    _cleanup(url)


def _cleanup(url: str) -> None:
    with connect(url) as conn:
        conn.execute("DELETE FROM chunks WHERE accession = %s", (ACCESSION,))
        conn.execute("DELETE FROM sections WHERE accession = %s", (ACCESSION,))
        conn.execute("DELETE FROM facts WHERE cik = %s", (CIK,))
        conn.execute("DELETE FROM filings WHERE accession = %s", (ACCESSION,))
        conn.execute("DELETE FROM companies WHERE cik = %s", (CIK,))


def question(**overrides: object) -> Question:
    payload: dict[str, object] = {
        "id": "t1",
        "category": "fact",
        "question": "？",
        "as_of": date(2025, 1, 1),
        "universe": [TICKER],
        "rubric": {"2": "ok", "0": "no"},
        "author_note": "integration fixture",
    }
    payload.update(overrides)
    return Question.model_validate(payload)


def test_init_schema_is_idempotent(database_url: str) -> None:
    init_schema(database_url)
    init_schema(database_url)


def test_existing_accessions_finds_seeded_filing(database_url: str) -> None:
    from pipelines.ingest import _existing_accessions

    assert _existing_accessions(database_url, [ACCESSION, "does-not-exist"]) == {ACCESSION}


def test_warehouse_totals_count_seeded_rows(database_url: str) -> None:
    from pipelines.ingest import _warehouse_totals

    totals = _warehouse_totals(database_url)
    assert totals["filings"] >= 1
    assert totals["facts"] >= 1
    assert 0.0 < totals["coverage"] <= 1.0


def test_citation_check_accepts_real_section(database_url: str) -> None:
    q = question(required_citations=[f"{TICKER}/10-K/FY2024#8"])
    assert _check_citations([q], database_url) == []


def test_citation_check_rejects_missing_section(database_url: str) -> None:
    q = question(required_citations=[f"{TICKER}/10-K/FY2024#99"])
    errors = _check_citations([q], database_url)
    assert any("not found" in e for e in errors)


def test_citation_check_rejects_non_canonical(database_url: str) -> None:
    q = question(required_citations=["some free text"])
    errors = _check_citations([q], database_url)
    assert any("not canonical" in e for e in errors)


def test_ledger_accepts_matching_gold(database_url: str) -> None:
    q = question(
        category="calculation",
        gold=Gold(value=100, unit="USD", tolerance=0.001),
        gold_source=GoldSource(kind="xbrl", cik=CIK, tag=TAG, period_end=date(2024, 9, 28)),
    )
    assert _check_ledger([q], database_url) == []


def test_ledger_rejects_wrong_gold(database_url: str) -> None:
    q = question(
        category="calculation",
        gold=Gold(value=123, unit="USD", tolerance=0.001),
        gold_source=GoldSource(kind="xbrl", cik=CIK, tag=TAG, period_end=date(2024, 9, 28)),
    )
    errors = _check_ledger([q], database_url)
    assert any("!=" in e for e in errors)


def test_stats_main_prints_warehouse(database_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    from pipelines.stats import main

    main()
    out = capsys.readouterr().out
    assert "FinTrace warehouse" in out
    assert "filings" in out
