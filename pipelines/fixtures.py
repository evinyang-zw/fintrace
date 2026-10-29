"""Load the synthetic adversarial filings into the same warehouse as the real ones.

The A1 questions are only answerable if their corpus is queryable by the same
tools. Reusing ``split_sections``/``chunk_text`` (rather than a bespoke loader)
is the point: if the parser breaks on a new 10-K, it breaks here too, visibly.

Accessions are the file stems, deliberately deterministic — the CI replay test
depends on the same doc_id in record and replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from rich.console import Console

from pipelines.chunking import chunk_text
from pipelines.config import Settings
from pipelines.db import connect, init_schema
from pipelines.extract import html_to_text
from pipelines.sections import split_sections

FIXTURE_DIR = Path("fixtures/adversarial")
console = Console()


@dataclass(frozen=True, slots=True)
class Fixture:
    filename: str
    ticker: str
    name: str
    form: str
    period_end: date
    filed_at: date
    fiscal_year: int

    @property
    def accession(self) -> str:
        return Path(self.filename).stem

    @property
    def cik(self) -> str:
        return f"FIX{self.ticker[:6].ljust(6, '0')}"


FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        "northwind-10k-fy2024.html",
        "NORTHWIND",
        "Northwind Analytics, Inc.",
        "10-K",
        date(2024, 12, 31),
        date(2025, 2, 14),
        2024,
    ),
    Fixture(
        "helios-10k-fy2024.html",
        "HELIOS",
        "Helios Instruments, Inc.",
        "10-K",
        date(2024, 9, 30),
        date(2024, 12, 15),
        2024,
    ),
    Fixture(
        "aurora-10k-fy2023.html",
        "AURORA",
        "Aurora Foods Corporation",
        "10-K",
        date(2023, 12, 31),
        date(2024, 2, 15),
        2023,
    ),
    Fixture(
        "aurora-10ka-fy2023.html",
        "AURORA",
        "Aurora Foods Corporation",
        "10-K/A",
        date(2023, 12, 31),
        date(2024, 9, 10),
        2023,
    ),
    Fixture(
        "meridian-10k-fy2024.html",
        "MERIDIAN",
        "Meridian Holdings, Inc.",
        "10-K",
        date(2024, 12, 31),
        date(2025, 2, 20),
        2024,
    ),
)


def ingest(database_url: str) -> dict[str, int]:
    init_schema(database_url)
    stats = {
        "companies": len({fixture.cik for fixture in FIXTURES}),
        "filings": 0,
        "sections": 0,
        "chunks": 0,
    }
    with connect(database_url) as conn:
        for fixture in FIXTURES:
            path = FIXTURE_DIR / fixture.filename
            if not path.is_file():
                raise FileNotFoundError(path)
            conn.execute(
                """
                INSERT INTO companies (cik, ticker, name, group_name, fiscal_year_end)
                VALUES (%s, %s, %s, 'fixture', %s)
                ON CONFLICT (cik) DO UPDATE SET name = EXCLUDED.name
                """,
                (fixture.cik, fixture.ticker, fixture.name, f"{fixture.period_end:%m-%d}"),
            )
            conn.execute(
                """
                INSERT INTO filings (accession, cik, form, period_end, filed_at,
                                     primary_doc, raw_path, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'fixture')
                ON CONFLICT (accession) DO UPDATE
                  SET period_end = EXCLUDED.period_end,
                      filed_at = EXCLUDED.filed_at,
                      raw_path = EXCLUDED.raw_path
                """,
                (
                    fixture.accession,
                    fixture.cik,
                    fixture.form,
                    fixture.period_end,
                    fixture.filed_at,
                    fixture.filename,
                    str(path),
                ),
            )
            conn.execute("DELETE FROM chunks WHERE accession = %s", (fixture.accession,))
            conn.execute("DELETE FROM sections WHERE accession = %s", (fixture.accession,))

            text = html_to_text(path.read_text(encoding="utf-8"))
            sections = split_sections(text, fixture.form)
            stats["filings"] += 1
            for section in sections:
                conn.execute(
                    """
                    INSERT INTO sections
                        (accession, item_code, heading, char_start, char_end, text)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        fixture.accession,
                        section.item_code,
                        section.heading,
                        section.char_start,
                        section.char_end,
                        section.text,
                    ),
                )
                stats["sections"] += 1
                for chunk in chunk_text(section.text):
                    conn.execute(
                        """
                        INSERT INTO chunks
                            (accession, filed_at, item_code, char_start, char_end, text, n_tokens)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            fixture.accession,
                            fixture.filed_at,
                            section.item_code,
                            section.char_start + chunk.char_start,
                            section.char_start + chunk.char_end,
                            chunk.text,
                            chunk.n_tokens,
                        ),
                    )
                    stats["chunks"] += 1
    return stats


def main() -> None:
    settings = Settings.from_env()
    stats = ingest(settings.database_url)
    console.print(
        f"[green]✓[/green] fixtures: {stats['companies']} companies / "
        f"{stats['filings']} filings / {stats['sections']} sections / {stats['chunks']} chunks"
    )


if __name__ == "__main__":
    main()
