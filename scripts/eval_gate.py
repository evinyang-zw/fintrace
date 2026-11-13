"""Compare a replay eval run against a committed baseline (W5 CI gate).

Replay keeps the gate deterministic and free: no provider, no key, no flakiness.
The price is that cassettes are tied to prompts and tool observations, so a
prompt change *must* re-record the baseline. That is the intended behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = Path("eval/datasets/smoke.jsonl")
DEFAULT_BASELINE = Path("eval/baselines/smoke.json")
DEFAULT_OUT = Path("reports/gate")

# A block means "do not merge"; a warning is shown but does not fail the build.
CITATION_DROP_BLOCK = 0.02
FALSE_REFUSAL_RISE_BLOCK = 0.05
CORRECTNESS_DROP_WARN = 0.05
COST_RISE_WARN = 0.30

app = typer.Typer(add_completion=False)


def _run_eval(dataset: Path, out: Path) -> Path:
    env = {
        **os.environ,
        "FINTRACE_LLM_MODE": "replay",
        "RETRIEVAL_TIER": "bm25",
        "LLM_API_KEY": os.environ.get("LLM_API_KEY", "replay-only"),
    }
    out.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "eval.runner",
        "--dataset",
        str(dataset),
        "--agent",
        "graph",
        "--judge",
        "llm",
        "--out",
        str(out),
    ]
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-4000:])
        print(result.stderr[-4000:])
        raise SystemExit(f"eval run failed with exit {result.returncode}")
    reports = sorted(out.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not reports:
        raise SystemExit(f"no report produced in {out}")
    return reports[-1]


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    metrics = payload.get("metrics") or payload
    return {
        "metrics": metrics,
        "meta": payload.get("meta", {}),
        "judge": payload.get("judge", ""),
    }


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_markdown(
    base: dict[str, Any], cur: dict[str, Any], blocks: list[str], warns: list[str]
) -> str:
    bm, cm = base["metrics"], cur["metrics"]
    rows = [
        ("正确率", bm.get("correctness", 0), cm.get("correctness", 0), CORRECTNESS_DROP_WARN),
        (
            "引用支撑率",
            bm.get("citation_support", 0),
            cm.get("citation_support", 0),
            CITATION_DROP_BLOCK,
        ),
        (
            "误拒率",
            bm.get("false_refusal_rate", 0),
            cm.get("false_refusal_rate", 0),
            FALSE_REFUSAL_RISE_BLOCK,
        ),
    ]
    lines = [
        "### FinTrace eval gate",
        "",
        f"- 数据集：`{cur.get('meta', {}).get('dataset', DEFAULT_DATASET)}`（n={cm.get('n')}）",
        f"- 判官：`{cur.get('judge')}` · 模式：replay（无网络、无费用）",
        "",
        "| 指标 | 基线 | 本次 | Δ |",
        "|---|---|---|---|",
    ]
    for label, before, after, _threshold in rows:
        delta = after - before
        sign = "+" if delta >= 0 else ""
        lines.append(f"| {label} | {_pct(before)} | {_pct(after)} | {sign}{delta * 100:.1f}pt |")
    cost_before = bm.get("cost_per_question", 0)
    cost_after = cm.get("cost_per_question", 0)
    lines.append(f"| 单题成本 | ${cost_before:.5f} | ${cost_after:.5f} | — |")
    lines.append("")
    if blocks:
        lines.append(f"**结论：阻断合并** — {'；'.join(blocks)}")
    elif warns:
        lines.append(f"**结论：通过（有警告）** — {'；'.join(warns)}")
    else:
        lines.append("**结论：通过** — 指标未越过阈值。")
    lines.extend(
        [
            "",
            "<sub>阈值：引用支撑 −2pt 阻断；误拒率 +5pt 阻断；正确率 −5pt 警告；"
            "成本 +30% 警告。</sub>",
        ]
    )
    return "\n".join(lines)


@app.command()
def main(
    dataset: Path = typer.Option(DEFAULT_DATASET),
    baseline: Path = typer.Option(DEFAULT_BASELINE),
    out: Path = typer.Option(DEFAULT_OUT),
    markdown: Path = typer.Option(Path("gate.md")),
) -> None:
    if not baseline.is_file():
        raise SystemExit(f"baseline missing: {baseline}")
    report = _run_eval(dataset, out)
    current = _load(report)
    base = _load(baseline)
    bm, cm = base["metrics"], current["metrics"]

    blocks: list[str] = []
    warns: list[str] = []
    if cm.get("citation_support", 0) <= bm.get("citation_support", 0) - CITATION_DROP_BLOCK:
        blocks.append(
            f"引用支撑率 {_pct(bm.get('citation_support', 0))} → {_pct(cm.get('citation_support', 0))}"
        )
    if (
        cm.get("false_refusal_rate", 0)
        >= bm.get("false_refusal_rate", 0) + FALSE_REFUSAL_RISE_BLOCK
    ):
        blocks.append(
            f"误拒率 {_pct(bm.get('false_refusal_rate', 0))} → {_pct(cm.get('false_refusal_rate', 0))}"
        )
    if cm.get("correctness", 0) <= bm.get("correctness", 0) - CORRECTNESS_DROP_WARN:
        warns.append(f"正确率 {_pct(bm.get('correctness', 0))} → {_pct(cm.get('correctness', 0))}")
    if cm.get("cost_per_question", 0) >= bm.get("cost_per_question", 0) * (1 + COST_RISE_WARN):
        warns.append(
            f"单题成本 ${bm.get('cost_per_question', 0):.5f} → ${cm.get('cost_per_question', 0):.5f}"
        )

    body = render_markdown(base, current, blocks, warns)
    markdown.write_text(body + "\n", encoding="utf-8")
    print(body)
    if blocks:
        raise SystemExit(1)


if __name__ == "__main__":
    app()
