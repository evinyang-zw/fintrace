"""Cassette semantics: record once, replay exactly, miss loudly."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.llm import CassetteClient, LLMError, LLMResult, Message


class FakeInner:
    name = "fake"

    def __init__(self, text: str = "hello") -> None:
        self.text = text
        self.calls = 0

    async def complete(self, messages, *, max_tokens=1200, json_mode=True, temperature=None):
        self.calls += 1
        return LLMResult(
            text=self.text,
            model="fake-model",
            tokens_in=10,
            tokens_cached=4,
            tokens_out=3,
            cost_usd=0.00001,
            peak=False,
            latency_ms=12,
            finish_reason="stop",
            created_at="2026-10-29T00:00:00+00:00",
        )


class ExplodingInner:
    name = "exploding"

    async def complete(self, *args, **kwargs):
        raise AssertionError("replay must not call the network")


async def test_record_then_replay(tmp_path: Path) -> None:
    messages = [Message("user", "test")]
    recorder = CassetteClient(
        FakeInner("recorded"), mode="record", model="fake-model", directory=tmp_path
    )
    first = await recorder.complete(messages)
    assert first.text == "recorded"
    assert len(list(tmp_path.glob("*.json"))) == 1

    replayer = CassetteClient(
        ExplodingInner(),  # type: ignore[arg-type]
        mode="replay",
        model="fake-model",
        directory=tmp_path,
    )
    second = await replayer.complete(messages)
    assert second == first


async def test_replay_miss_is_an_error(tmp_path: Path) -> None:
    replayer = CassetteClient(
        ExplodingInner(),  # type: ignore[arg-type]
        mode="replay",
        model="fake-model",
        directory=tmp_path,
    )
    with pytest.raises(LLMError, match="cassette miss"):
        await replayer.complete([Message("user", "never recorded")])


async def test_key_depends_on_sampling_params(tmp_path: Path) -> None:
    client = CassetteClient(FakeInner(), mode="record", model="fake-model", directory=tmp_path)
    await client.complete([Message("user", "x")], temperature=0.0)
    await client.complete([Message("user", "x")], temperature=0.7)
    assert len(list(tmp_path.glob("*.json"))) == 2


async def test_replay_rejects_a_different_served_model(tmp_path: Path) -> None:
    """A requested alias may route to another model; replaying it must fail loud."""
    messages = [Message("user", "x")]
    recorder = CassetteClient(
        FakeInner("recorded"), mode="record", model="alias", directory=tmp_path
    )
    key = recorder._key(messages, max_tokens=1200, json_mode=True, temperature=None)
    payload = {
        "key": key,
        "request": {"model": "alias", "messages": [m.as_dict() for m in messages]},
        "result": {
            "text": "old",
            "model": "deepseek-flash",  # served model differs from the requested alias
            "tokens_in": 1,
            "tokens_cached": 0,
            "tokens_out": 1,
            "cost_usd": 0.0,
            "peak": False,
            "latency_ms": 1,
            "finish_reason": "stop",
            "created_at": "2026-10-29T00:00:00+00:00",
        },
    }
    (tmp_path / f"{key}.json").write_text(__import__("json").dumps(payload))
    replayer = CassetteClient(
        ExplodingInner(),  # type: ignore[arg-type]
        mode="replay",
        model="alias",
        directory=tmp_path,
    )
    with pytest.raises(LLMError, match="served model"):
        await replayer.complete(messages)
