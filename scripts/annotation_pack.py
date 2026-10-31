"""Export a blind annotation pack for calibrating the LLM judge.

The pack must not contain the judge's verdict: a human who sees "the machine
said 2" anchors on it, and kappa then measures agreement with the anchor, not
with the rubric.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path
from typing import Any

DEFAULT_REPORTS = "reports/w3.1/*/*.json"
DATASET = Path("eval/datasets/gold_v1.jsonl")
OUT = Path("data/annotation/labels.csv")


def load_questions() -> dict[str, dict[str, Any]]:
    return {
        row["id"]: row
        for row in (
            json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line
        )
    }


def collect(
    reports: list[Path], questions: dict[str, dict[str, Any]], max_per_question: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for path in reports:
        report = json.loads(path.read_text(encoding="utf-8"))
        for item in report.get("items", []):
            grade = item.get("grade") or {}
            if grade.get("correctness") is None:
                continue  # refusals are graded by rules, not by this judge
            question = questions.get(item["id"])
            if question is None:
                continue
            qid = item["id"]
            if seen.get(qid, 0) >= max_per_question:
                continue
            seen[qid] = seen.get(qid, 0) + 1
            gold = question.get("gold") or {}
            gold_text = ""
            if gold.get("value") is not None:
                gold_text = (
                    f"{gold['value']:g} {gold.get('unit') or ''} ±{gold.get('tolerance', 0)}"
                )
            rows.append(
                {
                    "question_id": qid,
                    "category": question["category"],
                    "question": question["question"],
                    "key_points": " | ".join(question.get("key_points") or []),
                    "gold": gold_text,
                    "answer": str(item["answer"]["text"]).replace("\n", " "),
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", default=DEFAULT_REPORTS)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--per-category", type=int, default=12)
    parser.add_argument("--max-per-question", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    questions = load_questions()
    reports = sorted(Path().glob(args.reports))
    if not reports:
        raise SystemExit(f"no reports matched {args.reports}")

    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in collect(reports, questions, args.max_per_question):
        by_category.setdefault(row["category"], []).append(row)

    rng = random.Random(args.seed)
    sampled: list[dict[str, Any]] = []
    for category in sorted(by_category):
        pool = by_category[category]
        rng.shuffle(pool)
        sampled.extend(pool[: args.per_category])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "sample_id",
                "question_id",
                "category",
                "question",
                "key_points",
                "gold",
                "answer",
                "label",
                "reason",
            ],
        )
        writer.writeheader()
        for index, row in enumerate(sampled, start=1):
            writer.writerow({"sample_id": f"S{index:03d}", **row, "label": "", "reason": ""})

    counts = {
        category: sum(1 for r in sampled if r["category"] == category)
        for category in sorted(by_category)
    }
    print(f"wrote {len(sampled)} rows to {args.out}")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
