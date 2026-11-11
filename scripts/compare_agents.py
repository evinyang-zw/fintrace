"""Paired comparison of two eval reports (W4 A/B).

Reads the JSON reports produced by ``eval.runner`` and prints a markdown table
with paired deltas and bootstrap intervals. Pairing by question id is the point:
with 40-50 items a 2-point difference is usually one question.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from eval.bootstrap import paired_delta_ci

app = typer.Typer(add_completion=False)
console = Console()


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _vectors(
    report: dict[str, Any], category: str | None = None
) -> tuple[list[float], list[float]]:
    correct: list[float] = []
    citations: list[float] = []
    for item in report["items"]:
        if category and item["category"] != category:
            continue
        if item["grade"].get("correctness") is None:
            continue
        correct.append(1.0 if item["grade"]["correctness"] >= 1 else 0.0)
        citations.append(1.0 if item["grade"].get("citation_support", 0.0) >= 1.0 else 0.0)
    return correct, citations


def _delta(base: dict[str, Any], cur: dict[str, Any], category: str | None) -> dict[str, str]:
    b_correct, b_cite = _vectors(base, category)
    c_correct, c_cite = _vectors(cur, category)
    if not b_correct or not c_correct:
        return {"n": str(len(c_correct)), "correct": "—", "citation": "—"}
    if len(b_correct) != len(c_correct):
        raise typer.BadParameter(
            f"item sets differ ({len(b_correct)} vs {len(c_correct)}); same dataset required"
        )
    d_correct = sum(c_correct) / len(c_correct) - sum(b_correct) / len(b_correct)
    d_cite = sum(c_cite) / len(c_cite) - sum(b_cite) / len(b_cite)
    lo, hi = paired_delta_ci(b_correct, c_correct)
    lo_c, hi_c = paired_delta_ci(b_cite, c_cite)
    return {
        "n": str(len(c_correct)),
        "correct": f"{d_correct * 100:+.1f}pt [{lo * 100:+.1f}, {hi * 100:+.1f}]",
        "citation": f"{d_cite * 100:+.1f}pt [{lo_c * 100:+.1f}, {hi_c * 100:+.1f}]",
    }


@app.command()
def main(
    baseline: Path = typer.Argument(..., help="baseline report .json"),
    current: Path = typer.Argument(..., help="current report .json"),
    category: str = typer.Option("", help="只比某一类，如 multi_hop"),
) -> None:
    base = _load(baseline)
    cur = _load(current)
    if base["agent"] == cur["agent"]:
        console.print("[yellow]warning: both reports are from the same agent[/yellow]")

    table = Table(title=f"{cur['agent']} - {base['agent']}")
    table.add_column("subset")
    table.add_column("n", justify="right")
    table.add_column("correctness Δ", justify="right")
    table.add_column("citation Δ", justify="right")

    subsets: list[str | None] = [None]
    if not category:
        subsets += sorted({item["category"] for item in cur["items"]})
    for subset in subsets:
        if subset and category and subset != category:
            continue
        row = _delta(base, cur, subset)
        table.add_row(subset or "all", row["n"], row["correct"], row["citation"])

    console.print(table)
    bm, cm = base["metrics"], cur["metrics"]
    console.print(
        f"cost/q: ${bm['cost_per_question']:.5f} -> ${cm['cost_per_question']:.5f} | "
        f"p95: {bm['latency_p95_ms']:.0f}ms -> {cm['latency_p95_ms']:.0f}ms | "
        f"judge: {base['judge']} vs {cur['judge']}"
    )


if __name__ == "__main__":
    app()
