"""15-second rerank gate: the endpoint must rank a relevant passage first.

Run before spending money on a 20-candidate pool per question. Prints scores but
never the API key.
"""

from __future__ import annotations

import sys

from rich.console import Console

from agent.rerank import DashScopeReranker, RerankError
from pipelines.config import Settings

QUERY = "What was total net sales in fiscal 2024?"
DOCUMENTS = [
    "Total net sales were $391,035 million for fiscal 2024.",
    "The company repurchased shares under its buyback program.",
    "Our headquarters are in Cupertino, California.",
]


def main() -> int:
    console = Console()
    settings = Settings.from_env()
    key = settings.resolved_rerank_key
    if not key:
        console.print("[red]DASHSCOPE_API_KEY is empty[/red]")
        return 2
    reranker = DashScopeReranker(url=settings.rerank_url, api_key=key, model=settings.rerank_model)
    try:
        ranked = reranker.rerank(QUERY, DOCUMENTS, top_n=3)
    except RerankError as exc:
        console.print(f"[red]rerank failed: {exc}[/red]")
        return 1
    for index, score in ranked:
        console.print(f"  {score:.4f}  [{index}] {DOCUMENTS[index][:70]}")
    if not ranked or ranked[0][0] != 0:
        console.print("[red]rerank did not put the relevant passage first[/red]")
        return 1
    console.print("[green]rerank smoke passed[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
