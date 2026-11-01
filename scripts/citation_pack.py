"""Export 30 claims for human calibration of the citation checker.

Each row is one sentence the checker will judge. The pack deliberately omits the
machine label; otherwise the annotator anchors on it and kappa measures agreement
with the anchor rather than with the rubric.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path
from typing import Any

from eval.citation import CitationChecker
from eval.schema import Answer, Question
from pipelines.config import Settings

DATASET = Path("eval/datasets/gold_v1.jsonl")
OUT_CSV = Path("data/annotation/citation_labels.csv")
OUT_JSON = Path("data/annotation/citation_answers.json")
CATEGORIES = ["fact", "multi_hop", "calculation", "comparison", "adversarial"]


def load_questions() -> dict[str, Question]:
    return {
        row["id"]: Question.model_validate(row)
        for row in (
            json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line
        )
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", default="reports/final/hybrid*/*.json")
    parser.add_argument("--per-category", type=int, default=6)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    checker = CitationChecker(Settings.from_env())
    questions = load_questions()
    pools: dict[str, list[dict[str, Any]]] = {category: [] for category in CATEGORIES}
    payload: dict[str, dict[str, Any]] = {}

    for path in sorted(Path().glob(args.reports)):
        report = json.loads(path.read_text(encoding="utf-8"))
        for item in report.get("items", []):
            answer = Answer.model_validate(item["answer"])
            question = questions.get(item["id"])
            if answer.refused or question is None:
                continue
            contexts, _ = checker.contexts(answer)
            fact_map = {fact.handle: fact for fact in answer.facts}
            for index, context in enumerate(contexts):
                if not context.spans and not context.fact_handles:
                    continue
                if context.spans:
                    spans = "\n---\n".join(context.spans[:2])
                else:
                    spans = "\n---\n".join(
                        f"{handle}: tag={fact_map[handle].tag}, "
                        f"value={fact_map[handle].value:,.0f} {fact_map[handle].unit}, "
                        f"period_end={fact_map[handle].period_end}"
                        for handle in context.fact_handles
                        if handle in fact_map
                    )
                pools[question.category].append(
                    {
                        "question_id": item["id"],
                        "claim_index": index,
                        "category": question.category,
                        "claim": context.text,
                        "spans": spans[:1400],
                        "answer": answer.model_dump(mode="json"),
                    }
                )

    rng = random.Random(args.seed)
    sampled: list[dict[str, Any]] = []
    for category in CATEGORIES:
        pool = pools[category]
        rng.shuffle(pool)
        sampled.extend(pool[: args.per_category])

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "sample_id",
                "question_id",
                "claim_index",
                "category",
                "claim",
                "spans",
                "label",
                "reason",
            ],
        )
        writer.writeheader()
        for number, row in enumerate(sampled, start=1):
            sample_id = f"C{number:03d}"
            payload[sample_id] = {"question_id": row["question_id"], "answer": row["answer"]}
            writer.writerow(
                {
                    "sample_id": sample_id,
                    "question_id": row["question_id"],
                    "claim_index": row["claim_index"],
                    "category": row["category"],
                    "claim": row["claim"],
                    "spans": row["spans"],
                    "label": "",
                    "reason": "",
                }
            )
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    counts = {
        category: sum(1 for row in sampled if row["category"] == category)
        for category in CATEGORIES
    }
    print(f"wrote {len(sampled)} claims to {OUT_CSV} (+ {OUT_JSON})")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
