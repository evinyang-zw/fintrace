"""OpenAI-compatible chat client, with optional record/replay.

Two deliberate choices:

* raw ``httpx`` instead of the vendor SDK — DeepSeek's ``thinking`` switch is not
  part of the OpenAI schema, and the wire format is simple enough that owning the
  twenty lines is cheaper than working around a client that hides them;
* cost is computed from the *served* model and the server-side ``created``
  timestamp, because both the alias and the local clock can lie.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import httpx

from agent.pricing import Pricing, PricingError, load_pricing
from pipelines.config import Settings

Role = Literal["system", "user", "assistant"]
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
DEFAULT_TIMEOUT_S = 120.0


class LLMError(RuntimeError):
    """A call that cannot be retried into success."""


class EmptyResponseError(LLMError):
    """Provider returned no content (e.g. max_tokens eaten by reasoning tokens).

    Separate type because this one is worth *one* retry inside the loop, while a
    4xx or a malformed payload is not.
    """


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    model: str
    tokens_in: int
    tokens_cached: int
    tokens_out: int
    cost_usd: float
    peak: bool
    latency_ms: int
    finish_reason: str
    created_at: str  # ISO-8601, UTC

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_cached": self.tokens_cached,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "peak": self.peak,
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LLMResult:
        return cls(
            text=str(raw["text"]),
            model=str(raw["model"]),
            tokens_in=int(raw["tokens_in"]),
            tokens_cached=int(raw["tokens_cached"]),
            tokens_out=int(raw["tokens_out"]),
            cost_usd=float(raw["cost_usd"]),
            peak=bool(raw["peak"]),
            latency_ms=int(raw["latency_ms"]),
            finish_reason=str(raw["finish_reason"]),
            created_at=str(raw["created_at"]),
        )


class LLMClient(Protocol):
    name: str

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1200,
        json_mode: bool = True,
        temperature: float | None = None,
    ) -> LLMResult: ...


def _expect(payload: dict[str, Any], key: str, kind: type) -> Any:
    value = payload.get(key)
    if not isinstance(value, kind):
        raise LLMError(f"malformed response: {key!r} is {type(value).__name__}")
    return value


class OpenAICompatClient:
    """One POST to ``/chat/completions``, retried on transient failures."""

    name = "openai-compat"

    def __init__(
        self,
        settings: Settings,
        pricing: Pricing | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        attempts: int = 3,
    ) -> None:
        self.model = settings.llm_model
        self._base_url = settings.llm_base_url.rstrip("/")
        self._api_key = settings.llm_api_key
        self._thinking = settings.llm_thinking
        self._default_temperature = settings.llm_temperature
        self._pricing = pricing or load_pricing()
        self._timeout_s = timeout_s
        self._attempts = attempts
        if not self._api_key:
            raise LLMError("LLM_API_KEY is empty; put it in .env (see .env.example)")

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1200,
        json_mode: bool = True,
        temperature: float | None = None,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.as_dict() for m in messages],
            "max_tokens": max_tokens,
            "temperature": (self._default_temperature if temperature is None else temperature),
            "thinking": {"type": self._thinking},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json=payload,
                    )
            except httpx.TransportError as exc:  # network, DNS, timeout
                last_error = exc
                if attempt < self._attempts:
                    await asyncio.sleep(0.5 * 2**attempt + random.random() * 0.2)
                    continue
                raise LLMError(f"transport failure after {attempt} attempts: {exc}") from exc

            if response.status_code in RETRYABLE_STATUS and attempt < self._attempts:
                last_error = LLMError(f"HTTP {response.status_code}")
                await asyncio.sleep(0.5 * 2**attempt + random.random() * 0.2)
                continue
            if response.status_code >= 400:
                raise LLMError(
                    f"HTTP {response.status_code} from {self._base_url}: {response.text[:400]}"
                )
            return self._parse(response.json(), started)

        raise LLMError(f"exhausted retries: {last_error}")

    def _parse(self, payload: dict[str, Any], started: float) -> LLMResult:
        choices = _expect(payload, "choices", list)
        if not choices:
            raise LLMError("response has no choices")
        choice = cast(dict[str, Any], choices[0])
        message = _expect(choice, "message", dict)
        text = str(message.get("content") or "")
        finish_reason = str(choice.get("finish_reason") or "")

        usage_raw = payload.get("usage") or {}
        usage = cast(dict[str, Any], usage_raw)
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        cached = int(usage.get("prompt_cache_hit_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        served_model = str(payload.get("model") or self.model)

        created_raw = payload.get("created")
        created_at = (
            datetime.fromtimestamp(float(created_raw), tz=UTC)
            if isinstance(created_raw, (int, float))
            else datetime.now(tz=UTC)
        )
        try:
            cost = self._pricing.cost_usd(
                served_model,
                tokens_in_hit=cached,
                tokens_in_miss=max(prompt_tokens - cached, 0),
                tokens_out=output_tokens,
                at=created_at,
            )
        except PricingError as exc:
            raise LLMError(str(exc)) from exc

        if not text.strip():
            raise EmptyResponseError(f"empty content (finish_reason={finish_reason!r})")

        return LLMResult(
            text=text,
            model=served_model,
            tokens_in=prompt_tokens,
            tokens_cached=cached,
            tokens_out=output_tokens,
            cost_usd=cost,
            peak=self._pricing.is_peak(created_at),
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=finish_reason,
            created_at=created_at.isoformat(),
        )


class CassetteClient:
    """Record every call, replay it later without network or money.

    The key is built from the *request* (model, messages, sampling params), so a
    prompt change intentionally misses the cache — that is what makes replay an
    honest reproduction rather than a stale echo.
    """

    name = "cassette"

    def __init__(
        self,
        inner: LLMClient,
        *,
        mode: Literal["record", "replay"],
        model: str,
        directory: Path = Path("tests/cassettes"),
    ) -> None:
        self._inner = inner
        self._mode = mode
        self._model = model
        self._dir = directory

    def _key(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int,
        json_mode: bool,
        temperature: float | None,
    ) -> str:
        request = {
            "model": self._model,
            "messages": [[m.role, m.content] for m in messages],
            "max_tokens": max_tokens,
            "json_mode": json_mode,
            "temperature": temperature,
        }
        blob = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1200,
        json_mode: bool = True,
        temperature: float | None = None,
    ) -> LLMResult:
        key = self._key(
            messages, max_tokens=max_tokens, json_mode=json_mode, temperature=temperature
        )
        path = self._dir / f"{key}.json"
        if self._mode == "replay":
            if not path.is_file():
                raise LLMError(f"cassette miss: {path} (record it with FINTRACE_LLM_MODE=record)")
            raw = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
            result = LLMResult.from_dict(cast(dict[str, Any], raw["result"]))
            if result.model != self._model:
                raise LLMError(
                    f"cassette served model {result.model!r} != configured {self._model!r}; "
                    "re-record instead of silently reusing another model's trajectory"
                )
            return result

        result = await self._inner.complete(
            messages, max_tokens=max_tokens, json_mode=json_mode, temperature=temperature
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": key,
            "request": {
                "model": self._model,
                "messages": [m.as_dict() for m in messages],
                "max_tokens": max_tokens,
                "json_mode": json_mode,
                "temperature": temperature,
            },
            "result": result.to_dict(),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return result


def build_llm(settings: Settings, *, mode: str | None = None) -> LLMClient:
    """Live by default; ``FINTRACE_LLM_MODE=record|replay`` switches to cassettes."""
    resolved = mode or os.environ.get("FINTRACE_LLM_MODE", "live")
    client = OpenAICompatClient(
        settings, timeout_s=settings.llm_timeout_s, attempts=settings.llm_attempts
    )
    if resolved == "live":
        return client
    if resolved not in {"record", "replay"}:
        raise LLMError(f"unknown FINTRACE_LLM_MODE={resolved!r} (live|record|replay)")
    return CassetteClient(client, mode=cast(Any, resolved), model=settings.llm_model)
