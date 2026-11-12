"""Render eval results as a human-reviewable markdown report + machine JSON."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from eval.bootstrap import percentile_ci
from eval.metrics import ItemResult, Metrics
from eval.schema import CATEGORY_LABELS, Category

AGENT_LABEL = "目标：答案正确率 ↑、引用支撑率 ↑、误拒率 ↓"

_BASELINE_KEYS = {
    "correctness": "答案正确率",
    "citation_support": "引用支撑率",
    "recall_at_8": "召回@8",
    "numeric_accuracy": "数值准确率",
    "refusal_recall": "拒答召回",
    "false_refusal_rate": "误拒率",
    "hallucination_rate": "幻觉率",
}


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _ci(values: list[float], seed: int) -> str:
    low, high = percentile_ci(values, seed=seed)
    return f"[{low * 100:.1f}, {high * 100:.1f}]"


def _delta(value: float, baseline: dict[str, float] | None, key: str) -> str:
    if not baseline or key not in baseline:
        return "—"
    return f"{(value - baseline[key]) * 100:+.1f}pt"


def render_markdown(
    metrics: Metrics,
    results: list[ItemResult],
    *,
    agent: str,
    judge: str,
    dataset: str,
    seed: int = 0,
    meta: dict[str, str] | None = None,
    baseline: dict[str, float] | None = None,
) -> str:
    answerable = [r for r in results if not r.question.must_refuse]
    scored = [r for r in answerable if r.grade.correctness is not None]
    correctness = [1.0 if (r.grade.correctness or 0) >= 1 else 0.0 for r in scored]
    citations = [1.0 if r.grade.citation_support >= 1.0 else 0.0 for r in answerable]
    verification_failed = sum(1 for r in results if r.answer.verification_failed)

    lines = [
        f"# FinTrace 评测报告 · `{agent}`",
        "",
        f"- 数据集：`{dataset}`（{metrics.n} 题；可答 {metrics.answerable} / "
        f"计分 {metrics.scored} / 不可答 {metrics.unanswerable}）",
        f"- 判官：`{judge}`",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 说明：{AGENT_LABEL}",
    ]
    for key, value in (meta or {}).items():
        lines.append(f"- {key}：`{value}`")
    lines += [
        "",
        "## 总览",
        "",
        "| 指标 | 值 | Δ | 95% CI | 说明 |",
        "|---|---|---|---|---|",
        f"| 答案正确率 | {_pct(metrics.correctness)} | "
        f"{_delta(metrics.correctness, baseline, 'correctness')} | "
        f"{_ci(correctness, seed)} | 计分题中得分 ≥1 的比例 |",
        f"| 平均得分 | {metrics.mean_score:.2f} | — | — | 0–1，rubric 归一化 |",
        f"| 引用支撑率 | {_pct(metrics.citation_support)} | "
        f"{_delta(metrics.citation_support, baseline, 'citation_support')} | "
        f"{_ci(citations, seed)} | claim 级语义支撑，非结构匹配 |",
        f"| 召回@8（F2） | {_pct(metrics.recall_at_8)} | "
        f"{_delta(metrics.recall_at_8, baseline, 'recall_at_8')} | — | "
        f"n={metrics.recall_n}，section 级 |",
        f"| 数值准确率 | {_pct(metrics.numeric_accuracy)} | "
        f"{_delta(metrics.numeric_accuracy, baseline, 'numeric_accuracy')} | — | C1+C2，容差 0.5% |",
        f"| 拒答召回 | {_pct(metrics.refusal_recall)} | "
        f"{_delta(metrics.refusal_recall, baseline, 'refusal_recall')} | — | R1 中正确拒答的比例 |",
        f"| 误拒率 | {_pct(metrics.false_refusal_rate)} | "
        f"{_delta(metrics.false_refusal_rate, baseline, 'false_refusal_rate')} | — | 可答题中被拒的比例 |",
        f"| 幻觉率 | {_pct(metrics.hallucination_rate)} | "
        f"{_delta(metrics.hallucination_rate, baseline, 'hallucination_rate')} | — | R1 中被强行回答的比例 |",
        f"| 未过校验发布 | {verification_failed} | — | — | verification_failed=true 的答案数 |",
        f"| 单题成本 | ${metrics.cost_per_question:.4f} | — | — | LLM + 工具 |",
        f"| 延迟 p50 / p95 | {metrics.latency_p50_ms:.0f} / {metrics.latency_p95_ms:.0f} ms | — | — | 端到端 |",
        "",
        "## 分类别",
        "",
        "| 类别 | 题数 | 计分 | 作答数 | 正确率 | 引用支撑 |",
        "|---|---|---|---|---|---|",
    ]
    for category, item in metrics.by_category.items():
        label = CATEGORY_LABELS.get(cast(Category, category), category)
        rate = _pct(item.correctness) if item.scored else "—（未计分）"
        lines.append(
            f"| {label} | {item.n} | {item.scored} | {item.answered} | {rate} | "
            f"{_pct(item.citation_support)} |"
        )

    tags = sorted({tag for r in results for tag in r.question.tags})
    if tags:
        lines += [
            "",
            "## 按标签",
            "",
            "| 标签 | 题数 | 计分 | 正确率 | 引用支撑 |",
            "|---|---|---|---|---|",
        ]
        for tag in tags:
            subset = [r for r in results if tag in r.question.tags]
            scored_tag = [r for r in subset if r.grade.correctness is not None]
            correct_tag = sum(1 for r in scored_tag if (r.grade.correctness or 0) >= 1)
            cited_tag = sum(1 for r in subset if r.grade.citation_support >= 1.0)
            lines.append(
                f"| {tag} | {len(subset)} | {len(scored_tag)} | "
                f"{_pct(correct_tag / len(scored_tag) if scored_tag else 0.0)} | "
                f"{_pct(cited_tag / len(subset) if subset else 0.0)} |"
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

    lines += ["", "---", ""]
    if judge.startswith("rule"):
        lines += [
            "> `rule-stub` 只能判断拒答行为与引用是否存在，**不代表语义正确性**。",
            "> 语义判官与人工校准在 W3 接入。",
        ]
    elif judge.startswith("deterministic"):
        lines += [
            "> 确定性判官：数字与 gold 比对（单位归一化）；引用做结构校验",
            "> （span 存在 + 在本次证据集中 + 引文匹配）。**不做语义支撑判断**，那是 W3 的事。",
            "> 多跳（F2）类别未计分：W2 不引入 LLM 判官，未计分不等于 0 分。",
        ]
    else:
        lines += ["> 判官口径见上表。"]
    lines.append("")
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
    meta: dict[str, str] | None = None,
    baseline: dict[str, float] | None = None,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    stem = f"{stamp}-{agent}" if not seed else f"{stamp}-{agent}-s{seed}"
    markdown = render_markdown(
        metrics,
        results,
        agent=agent,
        judge=judge,
        dataset=dataset,
        seed=seed,
        meta=meta,
        baseline=baseline,
    )
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(markdown, encoding="utf-8")

    payload: dict[str, Any] = {
        "agent": agent,
        "judge": judge,
        "dataset": dataset,
        "seed": seed,
        "meta": meta or {},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": {
            "n": metrics.n,
            "answerable": metrics.answerable,
            "scored": metrics.scored,
            "unanswerable": metrics.unanswerable,
            "correctness": metrics.correctness,
            "mean_score": metrics.mean_score,
            "citation_support": metrics.citation_support,
            "numeric_accuracy": metrics.numeric_accuracy,
            "recall_at_8": metrics.recall_at_8,
            "recall_n": metrics.recall_n,
            "refusal_recall": metrics.refusal_recall,
            "false_refusal_rate": metrics.false_refusal_rate,
            "hallucination_rate": metrics.hallucination_rate,
            "cost_per_question": metrics.cost_per_question,
            "latency_p50_ms": metrics.latency_p50_ms,
            "latency_p95_ms": metrics.latency_p95_ms,
            "verification_failed": sum(1 for r in results if r.answer.verification_failed),
            "by_tag": {
                tag: {
                    "n": len([r for r in results if tag in r.question.tags]),
                    "correctness": (
                        sum(
                            1
                            for r in results
                            if tag in r.question.tags
                            and r.grade.correctness is not None
                            and (r.grade.correctness or 0) >= 1
                        )
                        / max(
                            1,
                            len(
                                [
                                    r
                                    for r in results
                                    if tag in r.question.tags and r.grade.correctness is not None
                                ]
                            ),
                        )
                    ),
                }
                for tag in sorted({tag for r in results for tag in r.question.tags})
            },
            "by_category": {
                name: {
                    "n": item.n,
                    "scored": item.scored,
                    "answered": item.answered,
                    "correctness": item.correctness,
                    "citation_support": item.citation_support,
                }
                for name, item in metrics.by_category.items()
            },
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
