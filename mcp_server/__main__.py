"""Run the FinTrace MCP tool server.

Two transports:

* ``stdio`` (default) — local development, and what W4's tests use;
* ``http`` — production: the API container and the tool container are separate
  processes, so they need streamable HTTP on a port.

The server stays stateless and read-only; handles are assigned by the client.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from agent.graph.state import fact_record, handle_record
from agent.retrieval import Retriever
from agent.tools import EvidenceStore, ToolExecutor
from pipelines.config import Settings

server = MCPServer(
    name="fintrace-tools",
    version="0.1.0",
    instructions="Read-only financial filing tools for FinTrace.",
)


def _call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    settings = Settings.from_env()
    store = EvidenceStore()
    # Must use the same retrieval tier as the in-process backend; otherwise the
    # MCP path silently degrades to BM25 when RETRIEVAL_TIER=hybrid.
    executor = ToolExecutor(
        settings.database_url, store, retrieval=Retriever.from_settings(settings)
    )
    outcome = executor.execute(tool, args)
    return {
        "ok": outcome.ok,
        "code": outcome.code,
        "message": outcome.message,
        "observation": outcome.to_observation(),
        "handles": [handle_record(h) for h in store.handles.values()],
        "facts": [fact_record(f) for f in store.facts.values()],
    }


@server.tool(
    name="search_filings",
    description=(
        "在指定公司的财报正文里检索，返回带句柄的片段。query 用自然语言即可"
        "（中文或英文都行；英文通常更准）。只有这里和 get_section 返回的片段才能被引用。"
    ),
)
async def search_filings(
    query: str,
    tickers: list[str],
    as_of: str,
    k: int = 8,
    form: str | None = None,
) -> dict[str, Any]:
    return _call(
        "search_filings",
        {"query": query, "tickers": tickers, "as_of": as_of, "k": k, "form": form},
    )


@server.tool(
    name="get_section",
    description="取某份文档的某一节全文（截断 6000 字符）。用于核对检索片段之外的细节。",
)
async def get_section(doc_id: str, item: str) -> dict[str, Any]:
    return _call("get_section", {"doc_id": doc_id, "item": item})


@server.tool(
    name="xbrl_fact",
    description=(
        "查询 XBRL 结构化财务数字——所有数字的唯一权威来源。"
        "tag_query 用自然语言（如 'revenue' / 'net income' / 'capital expenditure'），"
        "会返回最多 5 个候选（含期间、单位与事实句柄），自己选对的那个。"
        "答案里的数字必须引用对应的 [F#]。"
    ),
)
async def xbrl_fact(
    ticker: str,
    tag_query: str,
    as_of: str,
    period_end: str | None = None,
) -> dict[str, Any]:
    return _call(
        "xbrl_fact",
        {"ticker": ticker, "tag_query": tag_query, "as_of": as_of, "period_end": period_end},
    )


@server.tool(
    name="compute",
    description="四则运算，返回数值。不要心算大数或百分比，用这个工具。",
)
async def compute(expression: str) -> dict[str, Any]:
    return _call("compute", {"expression": expression})


def _http_app() -> Any:
    """Streamable HTTP app plus a plain health route for Docker/compose."""
    import os

    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    # The API container reaches this server as ``http://mcp:8001/mcp``; the MCP
    # SDK's DNS-rebinding guard rejects unknown Host headers by default.
    hosts = [
        item.strip()
        for item in os.environ.get(
            "MCP_ALLOWED_HOSTS", "mcp:8001,localhost:8001,127.0.0.1:8001"
        ).split(",")
        if item.strip()
    ]
    security = TransportSecuritySettings(
        allowed_hosts=hosts,
        allowed_origins=[f"http://{host}" for host in hosts],
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, transport_security=security
    )

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "server": "fintrace-tools"})

    app.add_route("/health", health, methods=["GET"])
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="FinTrace MCP tool server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    if args.transport == "stdio":
        asyncio.run(server.run_stdio_async())
        return

    import uvicorn

    print(json.dumps({"transport": "http", "host": args.host, "port": args.port}))
    uvicorn.run(_http_app(), host=args.host, port=args.port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
