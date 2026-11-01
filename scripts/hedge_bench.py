"""Benchmark hedging against a server that reproduces the provider's slow mode.

Real traffic is bimodal (~94% of calls in 1-2s, ~6% stalling 15-19s), and the
stalls come in windows — so an A/B against the live API can land in a fast
window and prove nothing. This local server forces the first request for each
prompt to take 10s and answers the duplicate immediately, which is exactly the
shape hedging is meant to exploit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from statistics import mean

from agent.llm import Message, OpenAICompatClient
from pipelines.config import Settings

SLOW_S = 10.0
SEEN: set[str] = set()
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence the server
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        key = hashlib.sha256(json.dumps(body["messages"], sort_keys=True).encode()).hexdigest()
        with LOCK:
            first = key not in SEEN
            SEEN.add(key)
        if first:
            time.sleep(SLOW_S)
        payload = {
            "id": "bench",
            "model": body["model"],
            "created": int(time.time()),
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def client(settings: Settings, port: int, hedge: float) -> OpenAICompatClient:
    patched = Settings(
        data_dir=settings.data_dir,
        database_url=settings.database_url,
        sec_user_agent=settings.sec_user_agent,
        llm_base_url=f"http://127.0.0.1:{port}",
        llm_model=settings.llm_model,
        llm_api_key="bench",
        llm_timeout_s=30,
        llm_attempts=1,
        llm_hedge_after_s=hedge,
    )
    return OpenAICompatClient(patched, timeout_s=30, attempts=1, hedge_after_s=hedge)


async def measure(instance: OpenAICompatClient, prefix: str, count: int) -> list[int]:
    latencies = []
    for index in range(count):
        started = time.perf_counter()
        await instance.complete([Message("user", f"{prefix}-{index}")])
        latencies.append(int((time.perf_counter() - started) * 1000))
    await instance.aclose()
    return latencies


async def main() -> None:
    settings = Settings.from_env()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        off = await measure(client(settings, port, 0), "off", 8)
        on = await measure(client(settings, port, 1.0), "on", 8)
    finally:
        server.shutdown()

    def p95(values: list[int]) -> int:
        return sorted(values)[int(0.95 * len(values)) - 1]

    print(f"off: p50={mean(off) / 1000:.2f}s p95={p95(off) / 1000:.2f}s max={max(off) / 1000:.2f}s")
    print(f"on : p50={mean(on) / 1000:.2f}s p95={p95(on) / 1000:.2f}s max={max(on) / 1000:.2f}s")
    assert mean(on) < 2000, "hedge did not cut the injected stall"
    assert mean(off) > 8000, "the injected stall did not fire"
    print("hedge bench passed")


if __name__ == "__main__":
    asyncio.run(main())
