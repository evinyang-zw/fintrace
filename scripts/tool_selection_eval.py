"""Does the tool *description* change which tool the model picks?

20 fixtures, two spec variants (terse vs current). Only one LLM call per case and
no tool execution, so the whole check costs cents. This is the W4 evidence for
"tool descriptions are part of the interface", not decoration.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from agent.llm import Message, build_llm
from agent.react import parse_action
from agent.tools import TOOL_SPECS
from pipelines.config import Settings

app = typer.Typer(add_completion=False)
console = Console()

TERSE = [
    {
        "name": "search_filings",
        "description": "搜索财报",
        "parameters": TOOL_SPECS[0]["parameters"],
    },
    {
        "name": "get_section",
        "description": "读章节",
        "parameters": TOOL_SPECS[1]["parameters"],
    },
    {
        "name": "xbrl_fact",
        "description": "查财务数据",
        "parameters": TOOL_SPECS[2]["parameters"],
    },
    {"name": "compute", "description": "算数", "parameters": TOOL_SPECS[3]["parameters"]},
]

CASES: list[tuple[str, str, list[str]]] = [
    (
        "微软 FY2024 年报里，管理层怎么解释云业务增长？",
        "search_filings",
        ["query", "tickers", "as_of"],
    ),
    ("苹果年报 Item 1A 讲了哪些风险？", "search_filings", ["query", "tickers", "as_of"]),
    ("沃尔玛年报里关于库存管理的段落", "search_filings", ["query", "tickers", "as_of"]),
    ("可口可乐在 10-K 里怎么描述汇率影响？", "search_filings", ["query", "tickers", "as_of"]),
    ("亚马逊年报中 AWS 的竞争风险表述", "search_filings", ["query", "tickers", "as_of"]),
    ("把苹果 FY2024 10-K 的 Item 7 全文调出来", "get_section", ["doc_id", "item"]),
    ("打开 MSFT 10-K Item 8 的完整内容", "get_section", ["doc_id", "item"]),
    ("我要看 NVDA 年报的 Item 1A 原文", "get_section", ["doc_id", "item"]),
    ("读取 KO 10-K 第 7 节", "get_section", ["doc_id", "item"]),
    ("给我 WMT 10-K 的 Item 8", "get_section", ["doc_id", "item"]),
    ("苹果 FY2024 的总营收是多少美元？", "xbrl_fact", ["ticker", "tag_query", "as_of"]),
    ("微软 FY2024 资本开支多少？", "xbrl_fact", ["ticker", "tag_query", "as_of"]),
    ("英伟达 FY2024 的净利润", "xbrl_fact", ["ticker", "tag_query", "as_of"]),
    ("好市多 FY2024 营业收入", "xbrl_fact", ["ticker", "tag_query", "as_of"]),
    ("可口可乐 FY2024 毛利润是多少", "xbrl_fact", ["ticker", "tag_query", "as_of"]),
    ("算一下 391035/1000000", "compute", ["expression"]),
    ("(245122-211915)/211915 等于多少", "compute", ["expression"]),
    ("把 87450 千美元换算成美元", "compute", ["expression"]),
    ("120 减 100 再除以 100", "compute", ["expression"]),
    ("88.5/245.1 的百分比", "compute", ["expression"]),
]

SYSTEM = """你是工具路由器。根据问题选择唯一一个工具，并给出参数。
只输出 JSON：{"action": "工具名", "action_input": {...}}
可用工具：
{tools}
"""


def _score(reply: str, expected: str, required: list[str]) -> tuple[bool, bool]:
    parsed = parse_action(reply)
    if parsed is None:
        return False, False
    action, args, _ = parsed
    tool_ok = action == expected
    args_ok = tool_ok and all(key in args for key in required)
    return tool_ok, args_ok


async def _run(specs: list[dict[str, Any]], model: str) -> tuple[int, int, int]:
    settings = Settings.from_env()
    llm = build_llm(settings)
    system = SYSTEM.replace("{tools}", json.dumps(specs, ensure_ascii=False, indent=1))
    tool_hits = args_hits = 0
    for question, expected, required in CASES:
        result = await llm.complete(
            [Message("system", system), Message("user", question)],
            max_tokens=300,
            json_mode=True,
            temperature=0.0,
        )
        tool_ok, args_ok = _score(result.text, expected, required)
        tool_hits += int(tool_ok)
        args_hits += int(args_ok)
    return tool_hits, args_hits, len(CASES)


@app.command()
def main(model: str = typer.Option("", help="覆盖 LLM_MODEL")) -> None:
    settings = Settings.from_env()
    chosen = model or settings.llm_model
    current = asyncio.run(_run(TOOL_SPECS, chosen))
    terse = asyncio.run(_run(TERSE, chosen))

    table = Table(title=f"tool selection ({chosen}, n={len(CASES)})")
    table.add_column("specs")
    table.add_column("tool", justify="right")
    table.add_column("tool+args", justify="right")
    table.add_row("current", f"{current[0]}/{current[2]}", f"{current[1]}/{current[2]}")
    table.add_row("terse", f"{terse[0]}/{terse[2]}", f"{terse[1]}/{terse[2]}")
    console.print(table)


if __name__ == "__main__":
    app()
