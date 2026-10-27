"""HTTP client with polite throttling, retries and an on-disk snapshot cache.

The cache is not an optimisation: it is the reproducibility boundary. Once a
run has populated ``data/raw``, every later stage (and CI fixtures) works
offline and produces identical results.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(
        self,
        cache_dir: Path,
        user_agent: str,
        *,
        min_interval_s: float = 0.15,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout_s,
            follow_redirects=True,
            transport=transport,
        )
        self.fetched = 0
        self.cached = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_text(self, url: str, cache_path: Path, *, refresh: bool = False) -> str:
        if cache_path.exists() and not refresh:
            self.cached += 1
            return cache_path.read_text(encoding="utf-8", errors="replace")

        text = self._request(url)
        _atomic_write(cache_path, text)
        return text

    def get_json(self, url: str, cache_path: Path, *, refresh: bool = False) -> Any:
        return json.loads(self.get_text(url, cache_path, refresh=refresh))

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)
        self._last_request_at = time.monotonic()

    def _request(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                response = self._client.get(url)
                if response.status_code == 429 or response.status_code >= 500:
                    raise FetchError(f"{url} -> HTTP {response.status_code}")
                response.raise_for_status()
            except (httpx.HTTPError, FetchError) as exc:
                last_error = exc
                time.sleep(0.4 * (2**attempt))
                continue
            self.fetched += 1
            return response.text
        raise FetchError(f"failed after {self.max_retries} attempts: {url}") from last_error


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
