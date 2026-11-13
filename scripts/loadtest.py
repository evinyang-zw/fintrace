"""Hit the deployed API with N concurrent runs and report what happened.

This is a *product* load test: it measures queueing + full agent runs, not HTTP
throughput. Acceptance is "no 5xx, every run reaches a terminal state, p95 under
the agreed budget" — see the W5 plan.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import typer

app = typer.Typer(add_completion=False)

QUESTIONS: list[tuple[str, list[str]]] = [
    ("苹果 FY2024 的总净营收是多少美元？", ["AAPL"]),
    ("微软 FY2024 的资本开支占营收比例是多少？", ["MSFT"]),
    ("英伟达 FY2024 的总营收是多少美元？", ["NVDA"]),
    ("可口可乐 FY2024 的营业利润是多少美元？", ["KO"]),
    ("对比微软和谷歌 FY2024 的资本开支强度。", ["MSFT", "GOOGL"]),
]

TERMINAL = {"done", "failed", "cancelled"}
DEFAULT_OUT = Path("reports")
STATUSES = {202, 429, 503, 401}


async def _one(
    client: httpx.AsyncClient,
    base: str,
    token: str,
    index: int,
    *,
    timeout_s: float,
) -> dict[str, Any]:
    question, universe = QUESTIONS[index % len(QUESTIONS)]
    started = time.perf_counter()
    record: dict[str, Any] = {"question": question}
    try:
        response = await client.post(
            f"{base}/api/ask",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": question, "universe": universe},
        )
        record["submit_status"] = response.status_code
        if response.status_code != 202:
            record.update(ok=False, error=response.text[:200])
            return record
        run_id = response.json()["run_id"]
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            detail = await client.get(
                f"{base}/api/runs/{run_id}", headers={"Authorization": f"Bearer {token}"}
            )
            body = detail.json()
            if body.get("status") in TERMINAL:
                record.update(
                    run_id=run_id,
                    status=body["status"],
                    ok=body["status"] == "done" and not body.get("error"),
                    answer_chars=len((body.get("answer") or {}).get("text") or ""),
                )
                break
            await asyncio.sleep(0.5)
        else:
            record.update(ok=False, error="client timeout waiting for terminal state")
    except Exception as exc:  # network errors are results too
        record.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return record


@app.command()
def main(
    base: str = typer.Option("http://127.0.0.1:8000", help="API base URL"),
    token: str = typer.Option(..., envvar="API_TOKEN"),
    concurrency: int = typer.Option(20),
    total: int = typer.Option(20),
    timeout_s: float = typer.Option(180.0, help="per-run client timeout"),
    out: Path = typer.Option(DEFAULT_OUT),
) -> None:
    async def run() -> int:
        limits = httpx.Limits(max_connections=concurrency + 10, max_keepalive_connections=10)
        async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
            semaphore = asyncio.Semaphore(concurrency)

            async def task(index: int) -> dict[str, Any]:
                async with semaphore:
                    return await _one(client, base.rstrip("/"), token, index, timeout_s=timeout_s)

            started = time.perf_counter()
            records = await asyncio.gather(*[task(i) for i in range(total)])
            wall_s = time.perf_counter() - started

        latencies = sorted(r["latency_ms"] for r in records if r.get("latency_ms"))
        failures = [r for r in records if not r.get("ok")]
        server_errors = [r for r in records if r.get("submit_status", 0) >= 500]
        summary = {
            "base": base,
            "concurrency": concurrency,
            "total": total,
            "wall_s": round(wall_s, 1),
            "ok": len(records) - len(failures),
            "failed": len(failures),
            "server_errors": len(server_errors),
            "p50_ms": round(statistics.median(latencies), 1) if latencies else 0,
            "p95_ms": round(latencies[int(len(latencies) * 0.95) - 1], 1) if latencies else 0,
            "max_ms": round(max(latencies), 1) if latencies else 0,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (out / f"loadtest-{stamp}.json").write_text(
            json.dumps({"summary": summary, "runs": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 1 if server_errors or failures else 0

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    app()
