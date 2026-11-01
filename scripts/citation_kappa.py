"""Compare human claim labels with the citation checker and report kappa.

Human labels are recorded as supported / partial / unsupported; they map to
2 / 1 / 0 so the same quadratic-weighted kappa used for the correctness judge
can be applied (the three levels are ordered by strength of support).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from collections import Counter
from pathlib import Path

from eval.citation import CitationChecker
from eval.schema import Answer
from pipelines.config import Settings
from scripts.kappa import weighted_kappa

LABEL_TO_SCORE = {"supported": 2, "partial": 1, "unsupported": 0}
SCORE_TO_LABEL = {value: key for key, value in LABEL_TO_SCORE.items()}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", type=Path, default=Path("data/annotation/citation_labels.csv"))
    parser.add_argument(
        "--answers", type=Path, default=Path("data/annotation/citation_answers.json")
    )
    args = parser.parse_args()

    rows = list(csv.DictReader(args.labels.open(encoding="utf-8")))
    missing = [
        row["sample_id"]
        for row in rows
        if row.get("label", "").strip().lower() not in LABEL_TO_SCORE
    ]
    if missing:
        raise SystemExit(
            f"{len(missing)} rows without supported/partial/unsupported: {missing[:5]}"
        )

    payload = json.loads(args.answers.read_text(encoding="utf-8"))
    checker = CitationChecker(Settings.from_env())
    human: list[int] = []
    machine: list[int] = []
    disagreements: list[tuple[str, str, str]] = []

    for row in rows:
        sample = row["sample_id"]
        answer = Answer.model_validate(payload[sample]["answer"])
        labels = await checker.label_claims(answer)
        index = int(row["claim_index"])
        if index >= len(labels):
            continue
        h = LABEL_TO_SCORE[row["label"].strip().lower()]
        m = LABEL_TO_SCORE[labels[index]]
        human.append(h)
        machine.append(m)
        if h != m:
            disagreements.append((sample, SCORE_TO_LABEL[h], SCORE_TO_LABEL[m]))

    kappa = weighted_kappa(human, machine)
    agreement = sum(1 for h, m in zip(human, machine, strict=True) if h == m) / len(human)
    print(f"n={len(human)}  exact agreement={agreement:.1%}  weighted kappa={kappa:.3f}")
    print("human:", dict(Counter(SCORE_TO_LABEL[x] for x in human)))
    print("machine:", dict(Counter(SCORE_TO_LABEL[x] for x in machine)))
    print(f"disagreements: {len(disagreements)}")
    for sample, h, m in disagreements[:10]:
        print(f"  {sample}: human={h} machine={m}")


if __name__ == "__main__":
    asyncio.run(main())
