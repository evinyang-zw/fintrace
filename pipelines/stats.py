"""Print what is actually in the warehouse.

`make ingest` is idempotent, so a warm re-run legitimately reports zero new
filings. This command shows the standing totals instead, which is what you want
in a demo and in an ops check.
"""

from __future__ import annotations

from typing import Any, cast

from rich.console import Console
from rich.table import Table

from pipelines.config import Settings
from pipelines.db import connect


def _as_int(value: object) -> int:
    """psycopg returns ``object`` for aggregate columns under strict typing."""
    return int(cast(Any, value))


def main() -> None:
    console = Console()
    settings = Settings.from_env()
    try:
        with connect(settings.database_url) as conn:
            totals = conn.execute(
                """
                SELECT
                    (SELECT count(*) FROM companies) AS companies,
                    (SELECT count(*) FROM filings)   AS filings,
                    (SELECT count(*) FROM sections)  AS sections,
                    (SELECT count(*) FROM chunks)    AS chunks,
                    (SELECT count(*) FROM facts)     AS facts
                """
            ).fetchone()
            covered = conn.execute(
                """
                SELECT count(DISTINCT f.accession) AS n
                FROM filings f
                JOIN sections s ON s.accession = f.accession
                WHERE s.item_code <> 'FULL'
                """
            ).fetchone()
            forms = conn.execute(
                "SELECT form, count(*) AS n FROM filings GROUP BY form ORDER BY n DESC"
            ).fetchall()
    except Exception as exc:
        console.print(f"[red]database unavailable:[/red] {exc}")
        raise SystemExit(1) from exc

    assert totals is not None and covered is not None
    filings = _as_int(totals["filings"])
    coverage = _as_int(covered["n"]) / filings if filings else 0.0

    table = Table(title="FinTrace warehouse")
    table.add_column("item")
    table.add_column("count", justify="right")
    for label, value in (
        ("companies", totals["companies"]),
        ("filings", totals["filings"]),
        ("sections", totals["sections"]),
        ("chunks", totals["chunks"]),
        ("XBRL facts", totals["facts"]),
    ):
        table.add_row(str(label), f"{_as_int(value):,}")
    table.add_row("section coverage", f"{coverage:.0%}")
    console.print(table)

    form_table = Table(title="by form")
    form_table.add_column("form")
    form_table.add_column("filings", justify="right")
    for row in forms:
        form_table.add_row(str(row["form"]), f"{_as_int(row['n']):,}")
    console.print(form_table)


if __name__ == "__main__":
    main()
