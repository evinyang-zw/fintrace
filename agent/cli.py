"""``make ask`` — run one ad-hoc question through the agent and print the evidence."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from agent.react import ReactAgent, ReactConfig
from eval.schema import Question
from pipelines.config import Settings

app = typer.Typer(add_completion=False)
console = Console()


@app.command()
def main(
    question: str = typer.Argument(..., help="中文问题"),
    universe: str = typer.Option("AAPL", help="逗号分隔的 ticker 列表"),
    as_of: str = typer.Option("", help="YYYY-MM-DD；默认今天"),
    period_spec: str = typer.Option("", help="如 AAPL=FY2024,MSFT=FY2024"),
    max_steps: int = typer.Option(7, help="步数上限"),
    budget: float = typer.Option(0.25, help="单题预算（美元）"),
    show_trace: bool = typer.Option(True, help="打印每步的工具调用"),
) -> None:
    settings = Settings.from_env()
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    tickers = [item.strip().upper() for item in universe.split(",") if item.strip()]
    periods = {}
    for pair in period_spec.split(","):
        if "=" in pair:
            ticker, period = pair.split("=", 1)
            periods[ticker.strip().upper()] = period.strip()

    agent = ReactAgent(settings, config=ReactConfig(max_steps=max_steps, budget_usd=budget))
    query = Question(
        id="adhoc",
        category="fact",
        question=question,
        as_of=as_of_date,
        universe=tickers,
        period_spec=periods,
    )
    answer = asyncio.run(agent.answer(query))

    console.print(
        Panel(answer.text, title="答案", border_style="green" if not answer.refused else "yellow")
    )
    if answer.citations:
        console.print("[bold]引用：[/bold]")
        for citation in answer.citations:
            console.print(
                f"  {citation.accession}#{citation.item_code} "
                f"[{citation.char_start}:{citation.char_end}] «{citation.quote[:80]}»"
            )
    usage = answer.usage
    console.print(
        f"[dim]cost=${usage.cost_usd:.5f} tokens={usage.tokens_in}/{usage.tokens_out} "
        f"cached={usage.tokens_cached} tools={usage.tool_calls} "
        f"latency={usage.latency_ms}ms trace={answer.trace_id}[/dim]"
    )

    if show_trace:
        path = Path(settings.data_dir) / "traces" / f"{answer.trace_id}.json"
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            for step in payload.get("steps", []):
                console.print(
                    f"[dim]{step['index']:>2}. {step['action']} "
                    f"{'ok' if step['ok'] else step['code']} "
                    f"({step['tool_latency_ms']}ms)[/dim]"
                )
            for note in payload.get("notes", []):
                console.print(f"[yellow]note: {note}[/yellow]")


if __name__ == "__main__":
    app()
