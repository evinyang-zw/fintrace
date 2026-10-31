"""Turn the ablation runs into one table, with paired bootstrap intervals.

Every tier answers the *same* questions, so the honest comparison is a paired
difference on per-question outcomes, not a difference of two means printed to
one decimal. This script refuses to pretend otherwise.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from statistics import mean
from typing import Any

TIER_ORDER = ["bm25", "hybrid", "hybrid_rerank"]


def load_runs(root: Path) -> dict[tuple[str, int], dict[str, Any]]:
    runs: dict[tuple[str, int], dict[str, Any]] = {}
    for path in sorted(root.glob("*-s*/2026-*-react*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        tier, seed_part = path.parent.name.rsplit("-s", 1)
        runs[(tier, int(seed_part))] = payload
    return runs


def scored(report: dict[str, Any]) -> dict[str, float]:
    """Per-question 1/0 for correctness, only where the judge actually scored."""
    out: dict[str, float] = {}
    for item in report.get("items", []):
        correctness = (item.get("grade") or {}).get("correctness")
        if correctness is not None:
            out[str(item["id"])] = 1.0 if int(correctness) >= 1 else 0.0
    return out


def citations(report: dict[str, Any]) -> dict[str, float]:
    return {
        str(item["id"]): float((item.get("grade") or {}).get("citation_support", 0.0))
        for item in report.get("items", [])
        if str(item.get("category")) != "unanswerable"
    }


def paired_ci(
    left: dict[str, float], right: dict[str, float], *, seed: int = 0, draws: int = 1000
) -> tuple[float, float, float]:
    keys = sorted(set(left) & set(right))
    if not keys:
        return 0.0, 0.0, 0.0
    diffs = [left[key] - right[key] for key in keys]
    point = mean(diffs)
    rng = random.Random(seed)
    samples = []
    for _ in range(draws):
        picks = [rng.choice(diffs) for _ in diffs]
        samples.append(mean(picks))
    samples.sort()
    return point, samples[int(0.025 * draws)], samples[int(0.975 * draws) - 1]


def f2_correctness(report: dict[str, Any]) -> float:
    rows = [
        item
        for item in report.get("items", [])
        if item.get("category") == "multi_hop"
        and (item.get("grade") or {}).get("correctness") is not None
    ]
    if not rows:
        return 0.0
    return mean(1.0 if int((r["grade"] or {})["correctness"]) >= 1 else 0.0 for r in rows)


def recall_at_8(report: dict[str, Any], questions: dict[str, dict[str, Any]]) -> tuple[float, int]:
    eligible = 0
    hit = 0
    for item in report.get("items", []):
        question = questions.get(str(item["id"]))
        if not question or question.get("category") != "multi_hop":
            continue
        required = set(question.get("required_citations") or [])
        if not required:
            continue
        eligible += 1
        seen = {
            str(key)
            for call in (item.get("answer") or {}).get("retrieval", [])
            for key in call.get("hits", [])
        }
        if required & seen:
            hit += 1
    return (hit / eligible if eligible else 0.0), eligible


def load_questions(path: str) -> dict[str, dict[str, Any]]:
    return {
        str(row["id"]): row
        for row in (
            json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line
        )
    }


def fmt(value: float) -> str:
    return f"{value * 100:.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("reports/ablation"))
    parser.add_argument("--out", type=Path, default=Path("reports/ablation/summary.md"))
    args = parser.parse_args()

    runs = load_runs(args.root)
    if not runs:
        raise SystemExit(f"no reports under {args.root}")
    dataset_path = next(iter(runs.values())).get("dataset", "eval/datasets/gold_v1.jsonl")
    questions = load_questions(str(dataset_path))

    lines = [
        "# W3 检索消融",
        "",
        "| 档位 | seed | 正确率 | 引用支撑 | F2 正确率 | recall@8 | 误拒率 | 单题成本 | p95 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for tier in TIER_ORDER:
        for seed in sorted(s for t, s in runs if t == tier):
            m = runs[(tier, seed)]["metrics"]
            recall, recall_n = recall_at_8(runs[(tier, seed)], questions)
            lines.append(
                f"| {tier} | {seed} | {fmt(float(m['correctness']))} | "
                f"{fmt(float(m['citation_support']))} | {fmt(f2_correctness(runs[(tier, seed)]))} | "
                f"{fmt(recall)} (n={recall_n}) | {fmt(float(m['false_refusal_rate']))} | "
                f"${float(m['cost_per_question']):.4f} | {float(m['latency_p95_ms']):.0f}ms |"
            )

    base = runs.get(("bm25", 0))
    if base is not None:
        lines += [
            "",
            f"## 配对差异（vs bm25, seed 0, n={len(scored(base))}）",
            "",
            "| 档位 | Δ 正确率 | 95% CI | Δ 引用支撑 | 95% CI |",
            "|---|---|---|---|---|",
        ]
        for tier in TIER_ORDER[1:]:
            other = runs.get((tier, 0))
            if other is None:
                continue
            dc, lo, hi = paired_ci(scored(other), scored(base))
            dcit, clo, chi = paired_ci(citations(other), citations(base))
            lines.append(
                f"| {tier} | {dc * 100:+.1f}pt | [{lo * 100:+.1f}, {hi * 100:+.1f}] | "
                f"{dcit * 100:+.1f}pt | [{clo * 100:+.1f}, {chi * 100:+.1f}] |"
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
