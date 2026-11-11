"""Validate the golden dataset before it is allowed to judge anything.

Three checks:
1. schema      - every line parses into ``Question``
2. structure   - unique ids, every category present, refusal/gold consistency
3. ledger      - gold numbers must be reproducible from the XBRL facts table

The ledger check is the cheap one that saves the most time: a mistyped digit in
``gold.value`` is caught here instead of silently costing two points of accuracy
for a week.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, cast

import typer
from rich.console import Console

from eval.runner import DEFAULT_DATASET, load_dataset
from eval.schema import CATEGORY_LABELS, Question
from pipelines.config import Settings
from pipelines.db import connect

app = typer.Typer(add_completion=False)
console = Console()

MIN_PER_CATEGORY = 5
MIN_TOTAL = 50

_CITATION_RE = re.compile(
    r"^(?P<ticker>[A-Z]{1,6})/(?P<form>10-[KQ](?:/A)?)/(?P<fy>FY\d{4})#(?P<item>\d{1,2}[A-C]?)$"
)


def _check_structure(questions: list[Question]) -> list[str]:
    errors: list[str] = []
    ids = Counter(q.id for q in questions)
    duplicates = [qid for qid, count in ids.items() if count > 1]
    if duplicates:
        errors.append(f"duplicate ids: {duplicates}")

    if len(questions) < MIN_TOTAL:
        errors.append(f"dataset has {len(questions)} questions, need >= {MIN_TOTAL}")

    counts = Counter(q.category for q in questions)
    for category in CATEGORY_LABELS:
        if counts[category] < MIN_PER_CATEGORY:
            errors.append(f"category {category} has {counts[category]} questions")

    adversarial_dir = Path("fixtures/adversarial")
    for q in questions:
        if q.corpus == "adversarial":
            if not q.corpus_ref:
                errors.append(f"{q.id}: adversarial question needs corpus_ref")
            elif not (adversarial_dir / q.corpus_ref).exists():
                errors.append(f"{q.id}: corpus file not found: {adversarial_dir / q.corpus_ref}")
        if "citation_trap" in q.tags and len(q.required_citations) != 1:
            errors.append(f"{q.id}: citation_trap needs exactly one required citation")
        if q.must_refuse and q.gold is not None:
            errors.append(f"{q.id}: unanswerable question must not define gold")
        if not q.must_refuse and q.category in {"calculation", "comparison"} and q.gold is None:
            errors.append(f"{q.id}: numeric question needs gold")
        if not q.rubric:
            errors.append(f"{q.id}: rubric is empty")
        if not q.author_note:
            errors.append(f"{q.id}: author_note missing (how would you debug a failure?)")
    return errors


def _latest_values(conn: Any, cik: str, tag: str, period_end: Any) -> list[float]:
    rows = conn.execute(
        """
        SELECT value FROM facts
        WHERE cik = %s AND tag = %s AND period_end = %s
        ORDER BY filed_at DESC LIMIT 5
        """,
        (cik, tag, period_end),
    ).fetchall()
    return [float(cast(Any, row["value"])) for row in rows]


def _check_citations(questions: list[Question], database_url: str) -> list[str]:
    """Every citation must point at a real, non-empty section in the corpus."""
    errors: list[str] = []
    targets = [
        (q.id, citation)
        for q in questions
        if q.corpus == "edgar"
        for citation in q.required_citations
    ]
    if not targets:
        return errors
    try:
        with connect(database_url) as conn:
            for question_id, raw in targets:
                match = _CITATION_RE.match(raw)
                if match is None:
                    errors.append(f"{question_id}: citation is not canonical: {raw!r}")
                    continue
                row = conn.execute(
                    """
                    SELECT count(*) AS n
                    FROM sections s
                    JOIN filings f ON f.accession = s.accession
                    JOIN companies c ON c.cik = f.cik
                    WHERE c.ticker = %s AND f.form = %s AND s.item_code = %s
                      AND length(s.text) > 0
                    """,
                    (match["ticker"], match["form"], match["item"]),
                ).fetchone()
                if row is None or int(cast(Any, row["n"])) == 0:
                    errors.append(f"{question_id}: cited section not found: {raw}")
    except Exception as exc:
        console.print(f"[yellow]skip citation check: database unavailable ({exc})[/yellow]")
    return errors


def _check_ledger(questions: list[Question], database_url: str) -> list[str]:
    errors: list[str] = []
    numeric = [
        q for q in questions if q.gold is not None and q.gold_source.kind in {"xbrl", "ratio"}
    ]
    if not numeric:
        return errors
    try:
        with connect(database_url) as conn:
            for q in numeric:
                assert q.gold is not None
                source = q.gold_source
                if not (source.cik and source.tag and source.period_end):
                    errors.append(f"{q.id}: xbrl gold_source needs cik/tag/period_end")
                    continue
                expected = q.gold.value
                if expected is None:
                    continue

                numerator = _latest_values(conn, source.cik, source.tag, source.period_end)
                if not numerator:
                    errors.append(
                        f"{q.id}: no fact for cik={source.cik} tag={source.tag} "
                        f"period_end={source.period_end}"
                    )
                    continue

                if source.kind == "ratio":
                    if not (source.denominator_cik and source.denominator_tag):
                        errors.append(f"{q.id}: ratio needs denominator_cik/tag")
                        continue
                    denominator = _latest_values(
                        conn,
                        source.denominator_cik,
                        source.denominator_tag,
                        source.denominator_period_end or source.period_end,
                    )
                    if not denominator or denominator[0] == 0:
                        errors.append(f"{q.id}: ratio denominator missing or zero")
                        continue
                    candidates = [numerator[0] / denominator[0]]
                else:
                    candidates = numerator

                best = min(abs(value - expected) / max(abs(expected), 1e-9) for value in candidates)
                if best > q.gold.tolerance:
                    errors.append(
                        f"{q.id}: gold {expected} != facts {candidates} (rel err {best:.4f})"
                    )
    except Exception as exc:
        console.print(f"[yellow]skip ledger check: database unavailable ({exc})[/yellow]")
    return errors


@app.command()
def main(
    dataset: Path = typer.Option(DEFAULT_DATASET),
    skip_ledger: bool = typer.Option(False, "--skip-ledger", help="不连数据库做数值对账"),
    skip_citations: bool = typer.Option(False, "--skip-citations", help="不连数据库校验引用"),
) -> None:
    questions = load_dataset(dataset)
    errors = _check_structure(questions)
    if not skip_ledger:
        errors += _check_ledger(questions, Settings.from_env().database_url)
    if not skip_citations:
        errors += _check_citations(questions, Settings.from_env().database_url)

    if errors:
        console.print(f"[red]✗ {len(errors)} problem(s)[/red]")
        for error in errors:
            console.print(f"  - {error}")
        raise typer.Exit(code=1)

    counts = Counter(q.category for q in questions)
    console.print(f"[green]✓ {len(questions)} questions valid[/green]")
    console.print(
        json.dumps({CATEGORY_LABELS[c]: counts[c] for c in CATEGORY_LABELS}, ensure_ascii=False)
    )


if __name__ == "__main__":
    app()
