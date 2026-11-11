"""MCP client backend: talks to ``mcp_server`` over stdio.

Two constraints shape this file:

* the server is stateless and returns raw evidence; **this** client assigns
  ``K#``/``F#`` handles, because a handle names a span within one agent run;
* the MCP stdio client owns an anyio cancel scope, so it must be entered and
  exited in the *same task*. The graph runs every node in a different task, so a
  long-lived worker task owns the connection and calls are queued to it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import CallToolResult

from agent.graph.agent import BackendResult
from agent.tools import EvidenceStore

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HANDLE = re.compile(r"\[([KF])(\d+)\]")
_JSON_HANDLE = re.compile(r'"([KF]\d+)"')


def _remap(text: str, mapping: dict[str, str]) -> str:
    def bracket(match: re.Match[str]) -> str:
        key = f"{match.group(1)}{match.group(2)}"
        return f"[{mapping.get(key, key)}]"

    def quoted(match: re.Match[str]) -> str:
        key = match.group(1)
        return f'"{mapping.get(key, key)}"'

    return _JSON_HANDLE.sub(quoted, _HANDLE.sub(bracket, text))


class _Worker:
    """Owns the stdio connection in one task and serialises tool calls."""

    def __init__(self, params: StdioServerParameters) -> None:
        self._params = params
        self._queue: asyncio.Queue[tuple[str | None, dict[str, Any], asyncio.Future[Any]]] = (
            asyncio.Queue()
        )
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._error: BaseException | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="mcp-worker")
        ready = asyncio.create_task(self._ready.wait())
        done, _ = await asyncio.wait(
            {ready, self._task}, return_when=asyncio.FIRST_COMPLETED, timeout=20
        )
        if ready not in done:
            raise RuntimeError(f"MCP server did not start: {self._error}")
        ready.cancel()

    async def _run(self) -> None:
        try:
            async with Client(self._params) as client:
                self._ready.set()
                while True:
                    name, args, future = await self._queue.get()
                    if name is None:
                        future.set_result(None)
                        return
                    try:
                        result = await client.call_tool(name, args)
                    except BaseException as exc:
                        if not future.done():
                            future.set_exception(exc)
                        continue
                    if not future.done():
                        future.set_result(result)
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            raise

    async def call(self, name: str, args: dict[str, Any]) -> CallToolResult:
        future: asyncio.Future[CallToolResult] = asyncio.get_running_loop().create_future()
        await self._queue.put((name, args, future))
        return await future

    async def close(self) -> None:
        if self._task is None:
            return
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put((None, {}, future))
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None


class MCPBackend:
    name = "mcp"

    def __init__(self, *, timeout_s: float = 5.0) -> None:
        self._timeout_s = timeout_s
        self._worker: _Worker | None = None

    async def _ensure(self) -> _Worker:
        if self._worker is not None:
            return self._worker
        env = {
            **os.environ,
            "PYTHONPATH": str(_REPO_ROOT),
            # Defence in depth: even a bug in the server cannot write.
            "PGOPTIONS": "-c default_transaction_read_only=on",
        }
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server"],
            cwd=str(_REPO_ROOT),
            env=env,
        )
        worker = _Worker(params)
        await worker.start()
        self._worker = worker
        return worker

    async def execute(self, store: EvidenceStore, name: str, args: dict[str, Any]) -> BackendResult:
        worker = await self._ensure()
        try:
            result = await asyncio.wait_for(worker.call(name, args), timeout=self._timeout_s)
        except TimeoutError:
            return BackendResult(
                False, "timeout", f"ERROR[timeout]: {name} exceeded {self._timeout_s}s"
            )
        except Exception as exc:
            return BackendResult(
                False, "mcp_error", f"ERROR[mcp_error]: {type(exc).__name__}: {exc}"
            )

        payload = result.structured_content
        if payload is None:
            first = result.content[0] if result.content else None
            text = getattr(first, "text", "{}")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return BackendResult(False, "mcp_error", f"ERROR[mcp_error]: {text[:200]}")
        if result.is_error:
            return BackendResult(False, "mcp_error", f"ERROR[mcp_error]: {payload}")

        mapping: dict[str, str] = {}
        for record in payload.get("handles", []):
            handle = store.add(
                accession=str(record["accession"]),
                item_code=str(record["item_code"]),
                char_start=int(record["char_start"]),
                char_end=int(record["char_end"]),
                quote=str(record["quote"]),
                label=str(record["label"]),
                filed_at=date.fromisoformat(str(record["filed_at"])),
            )
            mapping[str(record["name"])] = handle.name
        for record in payload.get("facts", []):
            fact = store.add_fact(
                ticker=str(record["ticker"]),
                tag=str(record["tag"]),
                value=float(record["value"]),
                unit=str(record["unit"]),
                period_end=date.fromisoformat(str(record["period_end"])),
                fiscal_period=(
                    str(record["fiscal_period"])
                    if record.get("fiscal_period") is not None
                    else None
                ),
            )
            mapping[str(record["name"])] = fact.name

        observation = _remap(str(payload.get("observation", "")), mapping)
        return BackendResult(bool(payload.get("ok")), payload.get("code"), observation)

    async def aclose(self) -> None:
        if self._worker is not None:
            await self._worker.close()
            self._worker = None
