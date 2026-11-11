"""``make graph-ask`` / ``make resume`` — the human-in-the-loop entry points.

Two separate entry points on purpose: ``Agent.answer()`` must stay a single
request/response (the eval runner depends on it), so review flows through
``start_review`` + ``resume`` instead of making ``answer()`` sometimes hang on a
human.
"""

from __future__ import annotations

import asyncio
from datetime import date

import typer
from rich.console import Console
from rich.panel import Panel

from agent.graph import GraphAgent, GraphConfig
from eval.schema import Answer, Question
from pipelines.config import Settings

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


def _print_answer(answer: Answer) -> None:
    console.print(
        Panel(answer.text, title="答案", border_style="green" if not answer.refused else "yellow")
    )
    for citation in answer.citations:
        console.print(
            f"  {citation.accession}#{citation.item_code} "
            f"[{citation.char_start}:{citation.char_end}] «{citation.quote[:80]}»"
        )
    if answer.verification_failed:
        console.print("[yellow]warning: 该答案未通过校验，已带标记返回[/yellow]")
    usage = answer.usage
    console.print(
        f"[dim]cost=${usage.cost_usd:.5f} tokens={usage.tokens_in}/{usage.tokens_out} "
        f"tools={usage.tool_calls} trace={answer.trace_id}[/dim]"
    )


@app.command()
def ask(
    question: str = typer.Argument(..., help="中文问题"),
    universe: str = typer.Option("AAPL", help="逗号分隔的 ticker"),
    as_of: str = typer.Option("", help="YYYY-MM-DD；默认今天"),
    review: bool = typer.Option(False, "--review", help="先中断，等人审"),
) -> None:
    settings = Settings.from_env()
    config = GraphConfig(review_mode="always" if review else "never")
    agent = GraphAgent(settings, config=config)
    query = Question(
        id="adhoc",
        category="fact",
        question=question,
        as_of=date.fromisoformat(as_of) if as_of else date.today(),
        universe=[item.strip().upper() for item in universe.split(",") if item.strip()],
    )

    async def run() -> None:
        try:
            if review:
                started = await agent.start_review(query)
                if started["status"] == "done":
                    _print_answer(started["answer"])
                    return
                payload = started["payload"]
                console.print(f"[bold]待审 thread:[/bold] {started['thread_id']}")
                console.print(
                    Panel(str((payload or {}).get("draft", {}).get("text", "")), title="草稿")
                )
                verification = (payload or {}).get("verification") or {}
                console.print(
                    f"校验通过: {verification.get('passed')} / {verification.get('problems')}"
                )
                console.print(
                    f"[dim]审阅：make resume THREAD={started['thread_id']} ACTION=approve[/dim]"
                )
            else:
                _print_answer(await agent.answer(query))
        finally:
            await agent.aclose()

    asyncio.run(run())


@app.command()
def resume(
    thread: str = typer.Argument(..., help="中断时返回的 thread_id"),
    action: str = typer.Option("approve", help="approve | edit | reject"),
    text: str = typer.Option("", help="action=edit 时的新正文"),
) -> None:
    settings = Settings.from_env()
    agent = GraphAgent(settings, config=GraphConfig(review_mode="always"))

    async def run() -> None:
        try:
            answer = await agent.resume(thread, {"action": action, "text": text})
            _print_answer(answer)
        finally:
            await agent.aclose()

    asyncio.run(run())


if __name__ == "__main__":
    app()
