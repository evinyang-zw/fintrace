"""Compare human labels with the LLM judge and report quadratic weighted kappa.

Run only after `data/annotation/labels.csv` has a 0/1/2 in every `label` cell.
The judge is called with the same rubric version as the runs, and its verdict is
cached, so re-running to inspect disagreements costs nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from collections import Counter
from pathlib import Path

from eval.llm_judge import LLMJudge
from eval.schema import Answer, Question
from pipelines.config import Settings

DATASET = Path("eval/datasets/gold_v1.jsonl")
LABELS = (0, 1, 2)


def load_questions() -> dict[str, Question]:
    return {
        row["id"]: Question.model_validate(row)
        for row in (
            json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line
        )
    }


def weighted_kappa(human: list[int], machine: list[int]) -> float:
    size = len(LABELS)
    observed = [[0] * size for _ in range(size)]
    for h, m in zip(human, machine, strict=True):
        observed[h][m] += 1
    total = len(human)
    row = [sum(observed[i]) for i in range(size)]
    col = [sum(observed[i][j] for i in range(size)) for j in range(size)]
    numerator = sum((i - j) ** 2 * observed[i][j] for i in range(size) for j in range(size))
    denominator = sum(
        (i - j) ** 2 * row[i] * col[j] / total for i in range(size) for j in range(size)
    )
    return 1.0 - numerator / denominator if denominator else 0.0


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", type=Path, default=Path("data/annotation/labels.csv"))
    args = parser.parse_args()

    rows = list(csv.DictReader(args.labels.open(encoding="utf-8")))
    missing = [
        row["sample_id"] for row in rows if row.get("label", "").strip() not in {"0", "1", "2"}
    ]
    if missing:
        raise SystemExit(f"{len(missing)} rows without a 0/1/2 label: {missing[:5]}")

    questions = load_questions()
    judge = LLMJudge(Settings.from_env())
    human: list[int] = []
    machine: list[int] = []
    disagreements: list[tuple[str, int, int]] = []
    for row in rows:
        question = questions[row["question_id"]]
        verdict = await judge.grade(question, Answer(text=row["answer"]))
        if verdict.correctness is None:
            continue
        h = int(row["label"])
        human.append(h)
        machine.append(int(verdict.correctness))
        if h != verdict.correctness:
            disagreements.append((row["sample_id"], h, int(verdict.correctness)))

    kappa = weighted_kappa(human, machine)
    agreement = sum(1 for h, m in zip(human, machine, strict=True) if h == m) / len(human)
    print(f"n={len(human)}  exact agreement={agreement:.1%}  quadratic weighted kappa={kappa:.3f}")
    print("human distribution:", dict(Counter(human)))
    print("judge distribution:", dict(Counter(machine)))
    print(f"disagreements: {len(disagreements)}")
    for sample, h, m in disagreements[:10]:
        print(f"  {sample}: human={h} judge={m}")


if __name__ == "__main__":
    asyncio.run(main())
