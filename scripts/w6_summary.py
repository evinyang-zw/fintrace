#!/usr/bin/env python3
"""Aggregate W6 dev/holdout eval reports into the final numbers.

The repo's per-run reports are per-seed; the W6 report needs four seeds merged,
the 50-question historical subset, and the one-shot holdout. This script keeps
that arithmetic in one place so the report never hand-adds.

Usage:
  PYTHONPATH=. uv run python scripts/w6_summary.py \
    --dev 'reports/w6-dev/seed*/2026-*-graph.json' \
    --holdout 'reports/w6-holdout/seed*/2026-*-graph.json' \
    --out reports/w6-final/summary.json
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from eval.bootstrap import percentile_ci
from eval.metrics import ItemResult, Metrics, aggregate
from eval.schema import Answer, Grade, Question


def load_questions(path: Path) -> dict[str, Question]:
    out: dict[str, Question] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = Question.model_validate_json(line)
            out[q.id] = q
    return out


def load_run(path: Path, questions: dict[str, Question]) -> list[ItemResult]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    results: list[ItemResult] = []
    for item in raw["items"]:
        question = questions[item["id"]]
        answer = Answer.model_validate(item["answer"])
        grade = Grade.model_validate(item["grade"])
        results.append(ItemResult(question=question, answer=answer, grade=grade))
    return results


def correctness_values(results: list[ItemResult]) -> list[float]:
    return [
        1.0 if (r.grade.correctness or 0) >= 1 else 0.0
        for r in results
        if not r.question.must_refuse and r.grade.correctness is not None
    ]


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def merged_metrics(results: list[ItemResult]) -> dict[str, Any]:
    m: Metrics = aggregate(results)
    values = correctness_values(results)
    low, high = percentile_ci(values, seed=0)
    return {
        "n_items": len(results),
        "n_questions": len({r.question.id for r in results}),
        "correctness": m.correctness,
        "correctness_ci": [low, high],
        "citation_support": m.citation_support,
        "numeric_accuracy": m.numeric_accuracy,
        "refusal_recall": m.refusal_recall,
        "false_refusal_rate": m.false_refusal_rate,
        "hallucination_rate": m.hallucination_rate,
        "cost_per_question": m.cost_per_question,
        "llm_cost_per_question": m.llm_cost_per_question,
        "retrieval_cost_per_question": m.retrieval_cost_per_question,
        "latency_p50_ms": m.latency_p50_ms,
        "latency_p95_ms": m.latency_p95_ms,
        "recall_at_8": m.recall_at_8,
        "recall_n": m.recall_n,
        "by_category": {
            key: {
                "n": cat.n,
                "scored": cat.scored,
                "correctness": cat.correctness,
                "citation_support": cat.citation_support,
            }
            for key, cat in m.by_category.items()
        },
    }


def per_seed(paths: list[Path], questions: dict[str, Question]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        results = load_run(path, questions)
        m = aggregate(results)
        rows.append(
            {
                "file": str(path),
                "n": m.n,
                "correctness": m.correctness,
                "citation_support": m.citation_support,
                "numeric_accuracy": m.numeric_accuracy,
                "false_refusal_rate": m.false_refusal_rate,
                "cost_per_question": m.cost_per_question,
                "latency_p95_ms": m.latency_p95_ms,
            }
        )
    return rows


def per_question(results: list[ItemResult]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_id: dict[str, list[ItemResult]] = defaultdict(list)
    for r in results:
        by_id[r.question.id].append(r)
    for qid in sorted(by_id):
        runs = by_id[qid]
        hits = [1 if (r.grade.correctness or 0) >= 1 else 0 for r in runs]
        out.append(
            {
                "id": qid,
                "category": runs[0].question.category,
                "runs": len(runs),
                "correct": sum(hits),
                "rate": sum(hits) / len(hits),
                "refused": sum(1 for r in runs if r.answer.refused),
            }
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", required=True, help="glob for dev seed reports")
    parser.add_argument("--holdout", required=True, help="glob for holdout reports")
    parser.add_argument("--questions", default="eval/datasets/gold_v3.jsonl")
    parser.add_argument("--old-questions", default="eval/datasets/gold_v2.jsonl")
    parser.add_argument("--out", type=Path, help="write summary json here")
    args = parser.parse_args()

    questions = load_questions(Path(args.questions))
    old_ids = set(load_questions(Path(args.old_questions)))

    dev_paths = sorted(Path(p) for p in glob.glob(args.dev))
    hold_paths = sorted(Path(p) for p in glob.glob(args.holdout))
    if not dev_paths:
        raise SystemExit("no dev reports matched")

    dev_results: list[ItemResult] = []
    for path in dev_paths:
        dev_results.extend(load_run(path, questions))
    subset = [r for r in dev_results if r.question.id in old_ids]

    summary: dict[str, Any] = {
        "dev_files": [str(p) for p in dev_paths],
        "holdout_files": [str(p) for p in hold_paths],
        "per_seed": per_seed(dev_paths, questions),
        "dev_merged": merged_metrics(dev_results),
        "dev_50_subset": merged_metrics(subset) if subset else None,
        "dev_per_question": per_question(dev_results),
    }
    if hold_paths:
        hold_results: list[ItemResult] = []
        for path in hold_paths:
            hold_results.extend(load_run(path, questions))
        summary["holdout_merged"] = merged_metrics(hold_results)
        summary["holdout_per_question"] = per_question(hold_results)
        summary["holdout_per_seed"] = per_seed(hold_paths, questions)

    print("# W6 final summary\n")
    print("## Dev by seed\n")
    print("| seed file | n | correctness | citation | numeric | false refusal | $/q | p95 |")
    print("|---|---|---|---|---|---|---|---|")
    for row in summary["per_seed"]:
        print(
            f"| {Path(row['file']).parent.name} | {row['n']} | {pct(row['correctness'])} |"
            f" {pct(row['citation_support'])} | {pct(row['numeric_accuracy'])} |"
            f" {pct(row['false_refusal_rate'])} | ${row['cost_per_question']:.5f} |"
            f" {row['latency_p95_ms']:.0f}ms |"
        )
    for name, key in (
        ("Dev merged (60 x seeds)", "dev_merged"),
        ("50-question subset", "dev_50_subset"),
    ):
        block = summary.get(key)
        if not block:
            continue
        low, high = block["correctness_ci"]
        print(f"\n## {name}\n")
        print(f"- items: {block['n_items']} · questions: {block['n_questions']}")
        print(f"- correctness: **{pct(block['correctness'])}** 95% CI [{pct(low)}, {pct(high)}]")
        print(f"- citation support: {pct(block['citation_support'])}")
        print(f"- numeric accuracy: {pct(block['numeric_accuracy'])}")
        print(f"- false refusal: {pct(block['false_refusal_rate'])}")
        print(
            f"- cost/q: ${block['cost_per_question']:.5f} (llm ${block['llm_cost_per_question']:.5f}"
            f" + retrieval ${block['retrieval_cost_per_question']:.5f})"
        )
        print(f"- p50/p95: {block['latency_p50_ms']:.0f}/{block['latency_p95_ms']:.0f} ms")
        print("\n| category | n | scored | correctness | citation |")
        print("|---|---|---|---|---|")
        for cat, cell in sorted(block["by_category"].items()):
            print(
                f"| {cat} | {cell['n']} | {cell['scored']} | {pct(cell['correctness'])} |"
                f" {pct(cell['citation_support'])} |"
            )

    if "holdout_merged" in summary:
        block = summary["holdout_merged"]
        low, high = block["correctness_ci"]
        print("\n## Holdout (one shot)\n")
        print(f"- items: {block['n_items']} · questions: {block['n_questions']}")
        print(f"- correctness: **{pct(block['correctness'])}** 95% CI [{pct(low)}, {pct(high)}]")
        print(f"- citation support: {pct(block['citation_support'])}")
        print(f"- false refusal: {pct(block['false_refusal_rate'])}")
        print("\n| id | category | runs | correct | refused |")
        print("|---|---|---|---|---|")
        for row in summary["holdout_per_question"]:
            print(
                f"| {row['id']} | {row['category']} | {row['runs']} | {row['correct']} |"
                f" {row['refused']} |"
            )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
