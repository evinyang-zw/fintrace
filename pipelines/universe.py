"""Resolve the ticker universe to CIKs and persist it.

CIKs are never hand-written: format errors in a 10-digit id are the cheapest
possible way to lose an afternoon.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer
import yaml
from rich.console import Console

from pipelines.config import Settings
from pipelines.db import connect, init_schema
from pipelines.sources.base import CompanyRef
from pipelines.sources.edgar import EdgarSource
from pipelines.sources.http import Fetcher

CONFIG_DIR = Path("config")
LOCK_PATH = CONFIG_DIR / "universe.lock.json"

app = typer.Typer(add_completion=False)


def load_universe(path: Path = CONFIG_DIR / "universe.yaml") -> tuple[list[dict[str, str]], str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = raw["tickers"]
    return entries, str(raw.get("since", "2021-01-01"))


def resolve(entries: list[dict[str, str]], settings: Settings) -> list[CompanyRef]:
    tickers = [entry["ticker"] for entry in entries]
    with Fetcher(settings.raw_dir, settings.sec_user_agent) as fetcher:
        source = EdgarSource(fetcher, settings.raw_dir)
        companies = {c.ticker: c for c in source.list_companies(tickers)}
    return [
        CompanyRef(
            cik=companies[entry["ticker"]].cik,
            ticker=entry["ticker"],
            name=companies[entry["ticker"]].name,
            group=entry.get("group"),
        )
        for entry in entries
    ]


def load_lock() -> list[CompanyRef]:
    payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    return [CompanyRef(**row) for row in payload]


def write_lock(companies: list[CompanyRef]) -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(c) for c in companies]
    LOCK_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def upsert_companies(companies: list[CompanyRef], database_url: str) -> None:
    with connect(database_url) as conn, conn.cursor() as cur:
        cur.executemany(
            """
                INSERT INTO companies (cik, ticker, name, group_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (cik) DO UPDATE
                  SET ticker = EXCLUDED.ticker,
                      name = EXCLUDED.name,
                      group_name = EXCLUDED.group_name
                """,
            [(c.cik, c.ticker, c.name, c.group) for c in companies],
        )


@app.command()
def main(
    refresh: bool = typer.Option(False, help="忽略缓存，重新向 EDGAR 拉取"),
    no_db: bool = typer.Option(False, help="只写 lock 文件，不写数据库"),
) -> None:
    console = Console()
    settings = Settings.from_env()
    entries, _ = load_universe()

    if LOCK_PATH.exists() and not refresh:
        companies = load_lock()
        console.print(f"[dim]using cached {LOCK_PATH}[/dim]")
    else:
        console.print(f"[bold]resolving {len(entries)} tickers on EDGAR…[/bold]")
        companies = resolve(entries, settings)
        write_lock(companies)

    if not no_db:
        init_schema(settings.database_url)
        upsert_companies(companies, settings.database_url)

    table = "\n".join(f"  {c.ticker:<6} {c.cik}  {c.group or '-':<10} {c.name}" for c in companies)
    console.print(table)
    console.print(f"[green]✓[/green] {len(companies)} companies")


if __name__ == "__main__":
    app()
