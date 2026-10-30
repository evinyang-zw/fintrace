"""Run an agent over a golden dataset and produce a report."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agent.pricing import load_pricing
from agent.react import ReactAgent
from eval.agents.null import NullAgent
from eval.graders import DeterministicJudge
from eval.judge import RuleJudge
from eval.metrics import ItemResult, Metrics, aggregate
from eval.report import write_reports
from eval.schema import Agent, Judge, Question
from pipelines.config import Settings

DEFAULT_DATASET = Path("eval/datasets/gold_v0.jsonl")
DEFAULT_OUT = Path("reports")

AGENTS: dict[str, Callable[[], Agent]] = {
    "null": NullAgent,
    "react": lambda: ReactAgent(Settings.from_env()),
}
JUDGES: dict[str, Callable[[], Judge]] = {
    "rule": RuleJudge,
    "deterministic": lambda: DeterministicJudge(Settings.from_env()),
}

app = typer.Typer(add_completion=False)


def load_dataset(path: Path) -> list[Question]:
    questions: list[Question] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            questions.append(Question.model_validate_json(stripped))
        except Exception as exc:
            raise ValueError(f"{path}:{line_no}: invalid question: {exc}") from exc
    return questions


async def run(
    questions: list[Question],
    agent: Agent,
    judge: Judge,
    *,
    concurrency: int = 4,
    budget_usd: float | None = None,
    fail_fast: bool = False,
) -> list[ItemResult]:
    semaphore = asyncio.Semaphore(concurrency)
    spent = 0.0
    failures: list[str] = []

    async def one(question: Question) -> ItemResult | None:
        nonlocal spent
        async with semaphore:
            try:
                answer = await agent.answer(question)
                spent += answer.usage.cost_usd
                grade = await judge.grade(question, answer)
            except Exception as exc:
                if fail_fast:
                    raise
                failures.append(f"{question.id}: {exc}")
                return None
            return ItemResult(question=question, answer=answer, grade=grade)

    results: list[ItemResult] = []
    for coroutine in asyncio.as_completed([one(q) for q in questions]):
        result = await coroutine
        if result is not None:
            results.append(result)
        if budget_usd is not None and spent > budget_usd:
            raise RuntimeError(f"budget exceeded: spent ${spent:.4f} > ${budget_usd:.2f}")
    order = {q.id: i for i, q in enumerate(questions)}
    results.sort(key=lambda r: order[r.question.id])
    if failures:
        print(f"warnings: {len(failures)} item(s) failed and were skipped")
        for failure in failures[:10]:
            print(f"  - {failure}")
    return results


def print_summary(console: Console, metrics: Metrics) -> None:
    table = Table(title="eval summary")
    table.add_column("指标")
    table.add_column("值", justify="right")
    table.add_row("答案正确率", f"{metrics.correctness:.1%}")
    table.add_row("引用支撑率", f"{metrics.citation_support:.1%}")
    table.add_row("数值准确率", f"{metrics.numeric_accuracy:.1%}")
    table.add_row("拒答召回", f"{metrics.refusal_recall:.1%}")
    table.add_row("误拒率", f"{metrics.false_refusal_rate:.1%}")
    table.add_row("幻觉率", f"{metrics.hallucination_rate:.1%}")
    table.add_row("单题成本", f"${metrics.cost_per_question:.4f}")
    console.print(table)


@app.command()
def main(
    dataset: Path = typer.Option(DEFAULT_DATASET, help="golden dataset (jsonl)"),
    agent: str = typer.Option("null", help="agent name"),
    judge: str = typer.Option("deterministic", help="judge name"),
    limit: int = typer.Option(0, help="只跑前 N 题，0 为全部"),
    concurrency: int = typer.Option(4, help="并发数"),
    budget_usd: float = typer.Option(5.0, help="成本熔断（美元）"),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="任一题报错立即退出"),
    out: Path = typer.Option(DEFAULT_OUT, help="报告输出目录"),
    seed: int = typer.Option(0, help="bootstrap 随机种子"),
    baseline: Path | None = typer.Option(None, help="上一次报告的 JSON，用于 Δ 列"),
) -> None:
    console = Console()
    if not dataset.exists():
        raise typer.BadParameter(f"dataset not found: {dataset}")

    questions = load_dataset(dataset)
    if limit:
        questions = questions[:limit]
    if not questions:
        raise typer.BadParameter("dataset is empty")

    if agent not in AGENTS:
        raise typer.BadParameter(f"unknown agent: {agent} (available: {sorted(AGENTS)})")
    if judge not in JUDGES:
        raise typer.BadParameter(f"unknown judge: {judge} (available: {sorted(JUDGES)})")
    agent_impl = AGENTS[agent]()
    judge_impl = JUDGES[judge]()
    results = asyncio.run(
        run(
            questions,
            agent_impl,
            judge_impl,
            concurrency=concurrency,
            budget_usd=budget_usd,
            fail_fast=fail_fast,
        )
    )
    metrics = aggregate(results)
    print_summary(console, metrics)

    settings = Settings.from_env()
    meta: dict[str, str] = {}
    if agent == "react":
        served = [r.answer.usage.model for r in results if r.answer.usage.model]
        # Report the *served* model (what actually ran and was billed), not the
        # requested alias. A mismatch here silently makes reports incomparable.
        meta = {
            "model": max(set(served), key=served.count) if served else settings.llm_model,
            "pricing_version": load_pricing().version,
            "thinking": settings.llm_thinking,
        }
    baseline_metrics: dict[str, float] | None = None
    if baseline is not None:
        raw = json.loads(baseline.read_text(encoding="utf-8"))
        baseline_metrics = {
            key: float(value)
            for key, value in raw.get("metrics", {}).items()
            if isinstance(value, int | float)
        }

    md_path, json_path = write_reports(
        metrics,
        results,
        out,
        agent=agent_impl.name,
        judge=judge_impl.name,
        dataset=str(dataset),
        seed=seed,
        meta=meta,
        baseline=baseline_metrics,
    )
    console.print(f"[green]✓[/green] {md_path}")
    console.print(f"[green]✓[/green] {json_path}")

    # machine-readable summary on stdout for CI logs
    print(json.dumps({"correctness": metrics.correctness}, ensure_ascii=False))


if __name__ == "__main__":
    app()
