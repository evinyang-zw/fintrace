"""MCP client backend.

Two transports, one contract:

* **stdio** (default) — the server is spawned as a child process. The MCP stdio
  client owns an anyio cancel scope, so one worker task owns the connection and
  serialises calls.
* **HTTP** (``MCP_URL``) — the server is a separate container. Calls go through a
  small pool of sessions so twenty concurrent runs do not queue behind one
  connection.

In both cases the server is stateless and returns raw evidence; **this** client
assigns ``K#``/``F#`` handles, because a handle names a span within one run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, cast

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import CallToolResult

from agent.graph.agent import BackendResult
from agent.tools import EvidenceStore

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HANDLE = re.compile(r"\[([KF])(\d+)\]")
_JSON_HANDLE = re.compile(r'"([KF]\d+)"')


def _content_text(result: CallToolResult) -> str:
    for item in result.content or []:
        text = getattr(item, "text", None)
        if text:
            return str(text)
    return ""


def _remap(text: str, mapping: dict[str, str]) -> str:
    def bracket(match: re.Match[str]) -> str:
        key = f"{match.group(1)}{match.group(2)}"
        return f"[{mapping.get(key, key)}]"

    def quoted(match: re.Match[str]) -> str:
        key = match.group(1)
        return f'"{mapping.get(key, key)}"'

    return _JSON_HANDLE.sub(quoted, _HANDLE.sub(bracket, text))


def _payload_to_result(payload: dict[str, Any], store: EvidenceStore) -> BackendResult:
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
                str(record["fiscal_period"]) if record.get("fiscal_period") is not None else None
            ),
        )
        mapping[str(record["name"])] = fact.name

    for call in payload.get("search_calls") or []:
        store.search_calls.append(dict(call))
    usage = payload.get("usage") or {}
    store.embedding_tokens += int(usage.get("embedding_tokens") or 0)
    store.rerank_tokens += int(usage.get("rerank_tokens") or 0)

    observation = _remap(str(payload.get("observation", "")), mapping)
    return BackendResult(bool(payload.get("ok")), payload.get("code"), observation)


def _result_to_payload(result: CallToolResult) -> dict[str, Any] | None:
    payload = result.structured_content
    if payload is not None:
        return dict(payload)
    first = result.content[0] if result.content else None
    text = getattr(first, "text", "{}")
    try:
        return dict(json.loads(text))
    except json.JSONDecodeError:
        return None


class _StdioWorker:
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


class _HttpPool:
    """A few persistent sessions; each call borrows one so calls run in parallel."""

    def __init__(self, url: str, size: int = 6) -> None:
        self._url = url
        self._size = size
        self._clients: list[Any] = []
        self._free: asyncio.Queue[Any] = asyncio.Queue()

    async def start(self) -> None:
        if self._clients:
            return
        for _ in range(self._size):
            client = Client(self._url)
            await client.__aenter__()
            self._clients.append(client)
            self._free.put_nowait(client)

    async def call(self, name: str, args: dict[str, Any]) -> CallToolResult:
        client = await self._free.get()
        try:
            result = await client.call_tool(name, args)
            return cast(CallToolResult, result)
        finally:
            self._free.put_nowait(client)

    async def close(self) -> None:
        for client in self._clients:
            with contextlib.suppress(Exception):  # best effort shutdown
                await client.__aexit__(None, None, None)
        self._clients = []


class MCPBackend:
    name = "mcp"

    def __init__(
        self, *, url: str | None = None, timeout_s: float = 5.0, pool_size: int = 6
    ) -> None:
        self._url = url or os.environ.get("MCP_URL", "")
        self._timeout_s = timeout_s
        self._pool_size = pool_size
        self._stdio: _StdioWorker | None = None
        self._http: _HttpPool | None = None

    async def _ensure(self) -> _StdioWorker | _HttpPool:
        if self._url:
            if self._http is None:
                pool = _HttpPool(self._url, size=max(1, self._pool_size))
                await pool.start()
                self._http = pool
            return self._http
        if self._stdio is not None:
            return self._stdio
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
        worker = _StdioWorker(params)
        await worker.start()
        self._stdio = worker
        return worker

    async def execute(self, store: EvidenceStore, name: str, args: dict[str, Any]) -> BackendResult:
        backend = await self._ensure()
        try:
            result: CallToolResult = await asyncio.wait_for(
                backend.call(name, args), timeout=self._timeout_s
            )
        except TimeoutError:
            return BackendResult(
                False, "timeout", f"ERROR[timeout]: {name} exceeded {self._timeout_s}s"
            )
        except Exception as exc:
            return BackendResult(
                False, "mcp_error", f"ERROR[mcp_error]: {type(exc).__name__}: {exc}"
            )

        # MCP validates the JSON schema before our tool runs, so a missing
        # argument comes back as a protocol error. Map it onto the same
        # ``bad_args`` code the in-process tool would have returned; the message
        # still names the missing fields, which is what the model needs.
        if result.is_error:
            text = _content_text(result)
            code = (
                "bad_args"
                if "validation error" in text.lower() or "field required" in text.lower()
                else "mcp_error"
            )
            return BackendResult(False, code, f"ERROR[{code}]: {text}")

        payload = _result_to_payload(result)
        if payload is None:
            return BackendResult(False, "mcp_error", "ERROR[mcp_error]: unparseable tool result")
        return _payload_to_result(payload, store)

    async def aclose(self) -> None:
        if self._stdio is not None:
            await self._stdio.close()
            self._stdio = None
        if self._http is not None:
            await self._http.close()
            self._http = None
