"""The HTTP transport must behave exactly like stdio at the client boundary."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from agent.mcp_backend import MCPBackend
from agent.tools import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def mcp_url() -> Iterator[str]:
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "MCP_ALLOWED_HOSTS": f"127.0.0.1:{port},localhost:{port}",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server", "--transport", "http", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:  # pragma: no cover - startup failure is an environment problem
        proc.terminate()
        pytest.fail("MCP HTTP server did not start")
    yield f"{base}/mcp"
    proc.terminate()
    proc.wait(timeout=10)


def test_http_tool_call(mcp_url: str) -> None:
    async def run() -> None:
        backend = MCPBackend(url=mcp_url, pool_size=2)
        try:
            result = await backend.execute(
                EvidenceStore(), "compute", {"expression": "391035/1000000"}
            )
            assert result.ok is True
            assert "0.391035" in result.observation
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_http_bad_args_maps_to_bad_args(mcp_url: str) -> None:
    async def run() -> None:
        backend = MCPBackend(url=mcp_url, pool_size=1)
        try:
            result = await backend.execute(EvidenceStore(), "compute", {"expression": ""})
            assert result.ok is False
            assert result.code == "bad_args"
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_http_calls_are_parallel(mcp_url: str) -> None:
    async def run() -> None:
        backend = MCPBackend(url=mcp_url, pool_size=4)
        try:
            results = await asyncio.gather(
                *[
                    backend.execute(EvidenceStore(), "compute", {"expression": "1+1"})
                    for _ in range(8)
                ]
            )
            assert all(result.ok for result in results)
        finally:
            await backend.aclose()

    asyncio.run(run())
