"""Render eval results as a human-reviewable markdown report + machine JSON."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import cast

from eval.bootstrap import percentile_ci
from eval.metrics import ItemResult, Metrics
from eval.schema import CATEGORY_LABELS, Category

AGENT_LABEL = "目标：答案正确率 ↑、引用支撑率 ↑、误拒率 ↓"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _ci(values: list[float], seed: int) -> str:
    low, high = percentile_ci(values, seed=seed)
    return f"[{low * 100:.1f}, {high * 100:.1f}]"


def render_markdown(
    metrics: Metrics,
    results: list[ItemResult],
    *,
    agent: str,
    judge: str,
    dataset: str,
    seed: int = 0,
) -> str:
    answerable = [r for r in results if not r.question.must_refuse]
    correctness = [1.0 if (r.grade.correctness or 0) >= 1 else 0.0 for r in answerable]
    citations = [1.0 if r.grade.citation_support >= 1.0 else 0.0 for r in answerable]

    lines = [
        f"# FinTrace 评测报告 · `{agent}`",
        "",
        f"- 数据集：`{dataset}`（{metrics.n} 题，可答 {metrics.answerable} / 不可答 {metrics.unanswerable}）",
        f"- 判官：`{judge}`",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 说明：{AGENT_LABEL}",
        "",
        "## 总览",
        "",
        "| 指标 | 值 | 95% CI | 说明 |",
        "|---|---|---|---|",
        f"| 答案正确率 | {_pct(metrics.correctness)} | {_ci(correctness, seed)} | 可答题中得分 ≥1 的比例 |",
        f"| 平均得分 | {metrics.mean_score:.2f} | — | 0–1，rubric 归一化 |",
        f"| 引用支撑率 | {_pct(metrics.citation_support)} | {_ci(citations, seed)} | 分母为可答题数，无引用记 0 |",
        f"| 数值准确率 | {_pct(metrics.numeric_accuracy)} | — | C1+C2，容差 0.5% |",
        f"| 拒答召回 | {_pct(metrics.refusal_recall)} | — | R1 中正确拒答的比例 |",
        f"| 误拒率 | {_pct(metrics.false_refusal_rate)} | — | 可答题中被拒的比例 |",
        f"| 幻觉率 | {_pct(metrics.hallucination_rate)} | — | R1 中被强行回答的比例 |",
        f"| 单题成本 | ${metrics.cost_per_question:.4f} | — | LLM + 工具 |",
        f"| 延迟 p50 / p95 | {metrics.latency_p50_ms:.0f} / {metrics.latency_p95_ms:.0f} ms | — | 端到端 |",
        "",
        "## 分类别",
        "",
        "| 类别 | 题数 | 作答数 | 正确率 | 引用支撑 |",
        "|---|---|---|---|---|",
    ]
    for category, item in metrics.by_category.items():
        label = CATEGORY_LABELS.get(cast(Category, category), category)
        lines.append(
            f"| {label} | {item.n} | {item.answered} | {_pct(item.correctness)} | "
            f"{_pct(item.citation_support)} |"
        )

    failures = [
        r
        for r in results
        if (r.grade.correctness or 0) == 0 or r.answer.refused != r.question.must_refuse
    ]
    lines += ["", "## 失败样本", ""]
    if not failures:
        lines.append("（无）")
    else:
        lines += ["| id | 类别 | 判官备注 | 回答摘要 |", "|---|---|---|---|"]
        for result in failures[:15]:
            summary = result.answer.text.replace("\n", " ")[:60]
            note = result.grade.notes.replace("|", "/")
            lines.append(
                f"| `{result.question.id}` | {result.question.category} | {note} | {summary} |"
            )

    lines += [
        "",
        "> 判官为 `rule-stub` 时，正确率与引用支撑率只能反映“是否拒答/是否有引用”，",
        "> 不代表语义正确性。语义判官与人工校准在 W3 接入。",
        "",
    ]
    return "\n".join(lines)


def write_reports(
    metrics: Metrics,
    results: list[ItemResult],
    out_dir: Path,
    *,
    agent: str,
    judge: str,
    dataset: str,
    seed: int = 0,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    stem = f"{stamp}-{agent}"
    markdown = render_markdown(
        metrics, results, agent=agent, judge=judge, dataset=dataset, seed=seed
    )
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(markdown, encoding="utf-8")

    payload = {
        "agent": agent,
        "judge": judge,
        "dataset": dataset,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": {
            "n": metrics.n,
            "answerable": metrics.answerable,
            "unanswerable": metrics.unanswerable,
            "correctness": metrics.correctness,
            "mean_score": metrics.mean_score,
            "citation_support": metrics.citation_support,
            "numeric_accuracy": metrics.numeric_accuracy,
            "refusal_recall": metrics.refusal_recall,
            "false_refusal_rate": metrics.false_refusal_rate,
            "hallucination_rate": metrics.hallucination_rate,
            "cost_per_question": metrics.cost_per_question,
            "latency_p50_ms": metrics.latency_p50_ms,
            "latency_p95_ms": metrics.latency_p95_ms,
        },
        "items": [
            {
                "id": r.question.id,
                "category": r.question.category,
                "answer": r.answer.model_dump(mode="json"),
                "grade": r.grade.model_dump(mode="json"),
            }
            for r in results
        ],
    }
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return md_path, json_path
