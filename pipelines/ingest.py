"""Ingest filings + XBRL facts into Postgres.

Idempotent by design: re-running skips filings that already have sections unless
``--force`` is passed. Every network fetch goes through the snapshot cache, so a
second run is offline and byte-identical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import typer
from psycopg import Connection
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from pipelines.chunking import chunk_text
from pipelines.config import Settings
from pipelines.db import connect, init_schema
from pipelines.extract import html_to_text
from pipelines.sections import Section, split_sections
from pipelines.sources.base import CompanyRef, Fact, FilingRef
from pipelines.sources.edgar import EdgarSource
from pipelines.sources.http import Fetcher
from pipelines.universe import load_lock, load_universe, resolve, upsert_companies, write_lock

CONFIG_DIR = Path("config")
LOCK_PATH = CONFIG_DIR / "universe.lock.json"

FACT_BATCH = 5000

app = typer.Typer(add_completion=False)


@dataclass
class CompanyReport:
    ticker: str
    filings_seen: int = 0
    filings_ingested: int = 0
    filings_failed: int = 0
    sections: int = 0
    full_only: int = 0
    chunks: int = 0
    facts: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def section_coverage(self) -> float:
        return 1.0 - (self.full_only / self.filings_ingested) if self.filings_ingested else 0.0


def _already_ingested(conn: Connection[dict[str, object]], accession: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sections WHERE accession = %s LIMIT 1", (accession,)
    ).fetchone()
    return row is not None


def _insert_facts(database_url: str, facts: list[Fact]) -> int:
    if not facts:
        return 0
    rows = [
        (
            f.cik,
            f.tag,
            f.unit,
            f.period_start,
            f.period_end,
            f.fiscal_year,
            f.fiscal_period,
            f.value,
            f.filed_at,
            f.form,
            f.accn,
            f.frame,
        )
        for f in facts
    ]
    inserted = 0
    with connect(database_url) as conn, conn.cursor() as cur:
        for start in range(0, len(rows), FACT_BATCH):
            batch = rows[start : start + FACT_BATCH]
            cur.executemany(
                """
                    INSERT INTO facts (cik, tag, unit, period_start, period_end,
                                       fiscal_year, fiscal_period, value, filed_at,
                                       form, accn, frame)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                batch,
            )
            inserted += cur.rowcount if cur.rowcount > 0 else 0
    return inserted


def _store_filing(
    database_url: str,
    filing: FilingRef,
    raw_sha256: str,
    raw_path: Path,
    primary_doc: str,
    sections: list[Section],
) -> None:
    with connect(database_url) as conn:
        conn.execute(
            """
            INSERT INTO filings (accession, cik, form, period_end, filed_at,
                                 primary_doc, raw_path, raw_sha256)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (accession) DO UPDATE
              SET period_end = EXCLUDED.period_end,
                  raw_path = EXCLUDED.raw_path,
                  raw_sha256 = EXCLUDED.raw_sha256
            """,
            (
                filing.accession,
                filing.cik,
                filing.form,
                filing.period_end,
                filing.filed_at,
                primary_doc,
                str(raw_path),
                raw_sha256,
            ),
        )
        conn.execute("DELETE FROM chunks WHERE accession = %s", (filing.accession,))
        conn.execute("DELETE FROM sections WHERE accession = %s", (filing.accession,))
        for section in sections:
            conn.execute(
                """
                INSERT INTO sections (accession, item_code, heading, char_start, char_end, text)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    filing.accession,
                    section.item_code,
                    section.heading,
                    section.char_start,
                    section.char_end,
                    section.text,
                ),
            )
            for chunk in chunk_text(section.text):
                conn.execute(
                    """
                    INSERT INTO chunks (accession, item_code, char_start, char_end, text, n_tokens)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        filing.accession,
                        section.item_code,
                        section.char_start + chunk.char_start,
                        section.char_start + chunk.char_end,
                        chunk.text,
                        chunk.n_tokens,
                    ),
                )


@app.command()
def main(
    only: str = typer.Option("", help="只抓这些 ticker，逗号分隔"),
    since: str = typer.Option("", help="起始日期 YYYY-MM-DD，默认取 universe.yaml"),
    limit_filings: int = typer.Option(0, help="每家公司最多抓几份，0 为不限"),
    skip_facts: bool = typer.Option(False, help="跳过 XBRL facts"),
    force: bool = typer.Option(False, help="重抓已入库的 filing"),
) -> None:
    console = Console()
    settings = Settings.from_env()
    _, default_since = load_universe()
    since_date = date.fromisoformat(since or default_since)

    companies = load_lock() if LOCK_PATH.exists() else resolve(load_universe()[0], settings)
    if not LOCK_PATH.exists():
        write_lock(companies)
    if only:
        wanted = {t.strip().upper() for t in only.split(",") if t.strip()}
        companies = [c for c in companies if c.ticker in wanted]

    init_schema(settings.database_url)
    upsert_companies(companies, settings.database_url)

    reports: list[CompanyReport] = []
    with Fetcher(settings.raw_dir, settings.sec_user_agent) as fetcher:
        source = EdgarSource(fetcher, settings.raw_dir)
        for company in companies:
            report = _ingest_company(
                console,
                source,
                company,
                since_date,
                limit_filings,
                skip_facts,
                force,
                settings.database_url,
            )
            reports.append(report)

    _write_report(reports, settings, fetcher.fetched, fetcher.cached)
    _print_summary(console, reports)


def _ingest_company(
    console: Console,
    source: EdgarSource,
    company: CompanyRef,
    since_date: date,
    limit_filings: int,
    skip_facts: bool,
    force: bool,
    database_url: str,
) -> CompanyReport:
    report = CompanyReport(ticker=company.ticker)
    console.rule(f"[bold]{company.ticker}[/bold] {company.name}")

    if not skip_facts:
        facts = source.fetch_facts(company, since_date)
        report.facts = _insert_facts(database_url, facts)
        console.print(f"  facts: [green]{report.facts:,}[/green] rows")

    filings = source.list_filings(company, since_date, date.today())
    report.filings_seen = len(filings)
    if limit_filings:
        filings = filings[:limit_filings]

    if not filings:
        console.print("  [yellow]no filings found[/yellow]")
        return report

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(bar_width=30),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"  {company.ticker} filings", total=len(filings))
        for filing in filings:
            progress.update(task, description=f"  {company.ticker} {filing.form} {filing.filed_at}")
            try:
                if not force and _already_ingested_conn(database_url, filing.accession):
                    progress.advance(task)
                    continue
                raw = source.fetch_document(filing)
                text = html_to_text(raw.html)
                sections = split_sections(text, filing.form)
                _store_filing(
                    database_url,
                    filing,
                    raw.sha256,
                    raw.path,
                    filing.primary_doc,
                    sections,
                )
                report.filings_ingested += 1
                report.sections += len(sections)
                report.chunks += sum(len(chunk_text(s.text)) for s in sections)
                if len(sections) == 1 and sections[0].item_code == "FULL":
                    report.full_only += 1
            except Exception as exc:
                report.filings_failed += 1
                report.errors.append(f"{filing.accession} ({filing.form}): {exc}")
            progress.advance(task)

    return report


def _already_ingested_conn(database_url: str, accession: str) -> bool:
    with connect(database_url) as conn:
        return _already_ingested(conn, accession)


def _write_report(
    reports: list[CompanyReport], settings: Settings, fetched: int, cached: int
) -> None:
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fetched_requests": fetched,
        "cache_hits": cached,
        "companies": [
            r.__dict__ | {"section_coverage": round(r.section_coverage, 3)} for r in reports
        ],
    }
    (settings.report_dir / f"ingest-{stamp}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _print_summary(console: Console, reports: list[CompanyReport]) -> None:
    table = Table(title="ingest summary")
    for column in ("ticker", "filings", "sections", "coverage", "facts", "errors"):
        table.add_column(column, justify="right" if column != "ticker" else "left")
    for r in reports:
        table.add_row(
            r.ticker,
            f"{r.filings_ingested}/{r.filings_seen}",
            f"{r.sections}",
            f"{r.section_coverage:.0%}",
            f"{r.facts:,}",
            str(len(r.errors)),
        )
    console.print(table)
    total = sum(r.filings_ingested for r in reports)
    covered = sum(r.filings_ingested - r.full_only for r in reports)
    if total:
        console.print(f"[bold]section coverage: {covered / total:.0%}[/bold]")


if __name__ == "__main__":
    app()
