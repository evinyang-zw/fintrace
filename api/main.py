"""FinTrace HTTP service (W5).

Design notes that matter more than the route list:

* ``POST /api/ask`` returns 202 immediately. A run takes seconds, so the HTTP
  request only enqueues it; progress travels over SSE.
* The app is single-process (``--workers 1``). Queue, rate limits and SSE buffers
  are in memory; multiple workers would each own a slice of the truth.
* ``/api/events`` needs a token, but ``EventSource`` cannot set headers. ``/ask``
  therefore returns a short-lived *ticket* that authorises exactly one stream.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from api.config import ApiConfig
from api.db import RunStore
from api.events import EventBus
from api.observability import RunTracer
from api.worker import WorkerPool

log = logging.getLogger("fintrace.api")

STATIC = Path(__file__).with_name("static")
TICKER = re.compile(r"^[A-Z0-9.\-]{1,10}$")


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    universe: list[str] | str = Field(default_factory=lambda: ["AAPL"])
    as_of: date | None = None
    period_spec: dict[str, str] = Field(default_factory=dict)
    review: bool = False

    @field_validator("universe")
    @classmethod
    def _split(cls, value: list[str] | str) -> list[str]:
        items = value.split(",") if isinstance(value, str) else value
        out = [str(item).strip().upper() for item in items if str(item).strip()]
        if not out or len(out) > 8:
            raise ValueError("universe must contain 1-8 tickers")
        for ticker in out:
            if not TICKER.match(ticker):
                raise ValueError(f"bad ticker: {ticker}")
        return out


class ReviewRequest(BaseModel):
    action: str = Field(pattern="^(approve|reject|edit)$")
    text: str | None = Field(default=None, max_length=6000)


class RateLimiter:
    """Per-process sliding window; the daily cap is enforced from Postgres."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, ip: str, kind: str, per_minute: int) -> None:
        now = time.monotonic()
        window = self._hits[(ip, kind)]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= per_minute:
            raise HTTPException(status_code=429, detail="too many requests; retry in a minute")
        window.append(now)


def _client_ip(request: Request, trust_proxy: bool) -> str:
    # Trusting X-Forwarded-For by default lets anyone forge an IP and walk past
    # the rate limit. Only read it when we are actually behind a proxy.
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _question_payload(body: AskRequest, run_id: UUID) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"api-{str(run_id)[:8]}",
        "category": "fact",
        "question": body.question.strip(),
        "as_of": (body.as_of or date.today()).isoformat(),
        "universe": body.universe,
        "period_spec": body.period_spec,
    }
    return payload


def create_app(
    cfg: ApiConfig | None = None,
    *,
    agent_factory: Callable[[], Any] | None = None,
    review_agent_factory: Callable[[], Any] | None = None,
) -> FastAPI:
    config = cfg or ApiConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = RunStore(config.database_url)
        await store.open()
        reaped = await store.reap_stale()
        if reaped:
            log.warning("marked %s interrupted run(s) as failed", reaped)

        agents: list[Any] = []
        factory = agent_factory
        review_factory = review_agent_factory
        if factory is None:
            from agent.graph.agent import GraphAgent, GraphConfig
            from pipelines.config import Settings

            settings = Settings.from_env()

            def _make(mode: str) -> Any:
                return GraphAgent(
                    settings,
                    config=GraphConfig(review_mode=mode, timeout_s=config.run_timeout_s),
                )

            factory = lambda: _make("never")  # noqa: E731
            review_factory = (lambda: _make("on_failure")) if config.review_mode else factory

        agent = factory()
        agents.append(agent)
        review_agent = review_factory() if review_factory else agent
        if review_agent is not agent:
            agents.append(review_agent)

        bus = EventBus()
        tracer = RunTracer(config)
        pool = WorkerPool(
            store,
            bus,
            agent,
            review_agent,
            workers=config.workers,
            queue_size=config.queue_size,
            tracer=tracer,
        )
        await pool.start()

        app.state.cfg = config
        app.state.store = store
        app.state.bus = bus
        app.state.pool = pool
        app.state.tracer = tracer
        app.state.tickets = {}
        app.state.limiter = RateLimiter()
        try:
            yield
        finally:
            await pool.stop()
            for item in agents:
                close = getattr(item, "aclose", None)
                if close is not None:
                    await close()
            tracer.flush()
            await store.close()

    app = FastAPI(title="FinTrace API", version="0.5.0", lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self';"
            " connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    async def auth(request: Request, authorization: str | None = Header(default=None)) -> str:
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if not token or not secrets.compare_digest(token, config.api_token):
            if config.demo_token and token and secrets.compare_digest(token, config.demo_token):
                return "demo"
            raise HTTPException(status_code=401, detail="missing or invalid token")
        return "api"

    @app.post("/api/ask", status_code=202)
    async def ask(body: AskRequest, request: Request, kind: str = Depends(auth)) -> dict[str, Any]:
        state = request.app.state
        ip = _client_ip(request, config.trust_proxy)
        state.limiter.check(ip, kind, config.per_ip_per_minute)

        if await state.store.today_spend() >= config.daily_budget_usd:
            raise HTTPException(status_code=503, detail="daily budget exhausted; try tomorrow")
        used = await state.store.ip_usage_today(ip)
        cap = config.demo_per_day if kind == "demo" else config.per_ip_per_day
        if used >= cap:
            raise HTTPException(status_code=429, detail="daily quota for this address reached")
        if state.pool.full():
            raise HTTPException(
                status_code=429,
                detail="queue full",
                headers={"Retry-After": "30"},
            )

        run_id = uuid4()
        thread_id = f"api-{run_id}"
        await state.store.create(
            run_id,
            thread_id,
            _question_payload(body, run_id),
            review=bool(body.review and config.review_mode),
            client_ip=ip,
            token_kind=kind,
        )
        state.pool.submit_nowait(str(run_id))
        ticket = secrets.token_urlsafe(18)
        state.tickets[ticket] = str(run_id)
        return {"run_id": str(run_id), "ticket": ticket, "status": "queued"}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: UUID, request: Request, kind: str = Depends(auth)) -> dict[str, Any]:
        row = await request.app.state.store.get(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return _public_run(row)

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: UUID, request: Request, ticket: str = "") -> StreamingResponse:
        state = request.app.state
        expected = state.tickets.get(ticket)
        if not expected or expected != str(run_id):
            raise HTTPException(status_code=401, detail="invalid stream ticket")

        async def gen() -> AsyncIterator[bytes]:
            try:
                async for event in state.bus.stream(str(run_id)):
                    if event["type"] in {"done", "failed", "awaiting_review"}:
                        state.tickets.pop(ticket, None)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
            finally:
                state.tickets.pop(ticket, None)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/review")
    async def review(
        run_id: UUID, body: ReviewRequest, request: Request, kind: str = Depends(auth)
    ) -> dict[str, Any]:
        state = request.app.state
        row = await state.store.get(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        if row["status"] != "awaiting_review":
            raise HTTPException(
                status_code=409, detail=f"run is {row['status']}, not awaiting review"
            )
        decision: dict[str, Any] = {"action": body.action}
        if body.action == "edit":
            if not body.text or not body.text.strip():
                raise HTTPException(status_code=422, detail="edit needs text")
            decision["text"] = body.text.strip()

        answer = await state.pool.review_agent.resume(str(row["thread_id"]), decision)
        payload = answer.model_dump(mode="json")
        await state.store.finish(run_id, payload, answer.usage.model_dump(mode="json"))
        state.bus.publish(
            str(run_id), {"type": "done", "answer": payload, "usage": payload["usage"]}
        )
        return {"status": "done", "answer": payload}

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        state = request.app.state
        try:
            await state.store.ping()
            db_ok = True
        except Exception:
            db_ok = False
        return {
            "ok": db_ok,
            "db": db_ok,
            "queue_depth": state.pool.depth,
            "queue_capacity": state.pool.capacity,
            "budget_spent_usd": round(await state.store.today_spend(), 6),
            "budget_limit_usd": config.daily_budget_usd,
            "version": app.version,
        }

    @app.get("/api/stats")
    async def stats(request: Request, days: int = 7) -> dict[str, Any]:
        days = max(1, min(days, 90))
        return cast(dict[str, Any], await request.app.state.store.stats(days))

    @app.get("/api/reviews")
    async def reviews(request: Request, kind: str = Depends(auth)) -> list[dict[str, Any]]:
        rows = await request.app.state.store.list_pending()
        return [_public_run(row) for row in rows]

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/dashboard")
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC / "dashboard.html")

    @app.get("/static/{name}")
    async def static_file(name: str) -> FileResponse:
        path = (STATIC / name).resolve()
        if not str(path).startswith(str(STATIC.resolve())) or not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path)

    return app


def _public_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(row["id"]),
        "status": row["status"],
        "question": row["question"],
        "answer": row["answer"],
        "usage": row["usage"],
        "error": row["error"],
        "review": row["review"],
        "review_payload": row["review_payload"],
        "queued_at": row["queued_at"].isoformat() if row.get("queued_at") else None,
        "started_at": row["started_at"].isoformat() if row.get("started_at") else None,
        "finished_at": row["finished_at"].isoformat() if row.get("finished_at") else None,
    }


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), workers=1)
