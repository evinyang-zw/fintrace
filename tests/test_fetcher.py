from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pipelines.sources.http import Fetcher, FetchError


def _fetcher(tmp_path: Path, handler: object, **kwargs: object) -> Fetcher:
    return Fetcher(
        tmp_path,
        "test-agent",
        min_interval_s=0,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def test_cache_prevents_second_request(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="hello")

    cache = tmp_path / "nested" / "a.txt"
    with _fetcher(tmp_path, handler) as fetcher:
        assert fetcher.get_text("https://example.com/a", cache) == "hello"
        assert fetcher.get_text("https://example.com/a", cache) == "hello"
    assert calls["n"] == 1
    assert fetcher.fetched == 1
    assert fetcher.cached == 1


def test_retries_on_server_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pipelines.sources.http.time.sleep", lambda _: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, text="ok")

    with _fetcher(tmp_path, handler) as fetcher:
        assert fetcher.get_text("https://example.com/retry", tmp_path / "r.txt") == "ok"
    assert calls["n"] == 3


def test_gives_up_after_max_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pipelines.sources.http.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with _fetcher(tmp_path, handler) as fetcher, pytest.raises(FetchError):
        fetcher.get_text("https://example.com/fail", tmp_path / "f.txt")


def test_json_roundtrip(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"a": 1})

    with _fetcher(tmp_path, handler) as fetcher:
        payload = fetcher.get_json("https://example.com/j", tmp_path / "j.json")
    assert payload == {"a": 1}
