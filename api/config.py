"""HTTP service configuration (W5).

Kept separate from ``pipelines.config.Settings`` because the API has its own
concerns: tokens, quotas, queue sizes. Everything is env-overridable so the
same image runs locally and on the server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_DATABASE_URL = "postgresql://fintrace:fintrace@localhost:5433/fintrace"


def _dotenv() -> dict[str, str]:
    path = Path(".env")
    if not path.is_file():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


@dataclass(frozen=True, slots=True)
class ApiConfig:
    database_url: str
    api_token: str
    demo_token: str = ""
    workers: int = 6
    queue_size: int = 50
    per_ip_per_minute: int = 5
    per_ip_per_day: int = 30
    demo_per_day: int = 5
    daily_budget_usd: float = 5.0
    run_timeout_s: float = 120.0
    review_mode: bool = True
    trust_proxy: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    @classmethod
    def from_env(cls) -> ApiConfig:
        env = _dotenv()

        def pick(name: str, default: str = "") -> str:
            return os.environ.get(name) or env.get(name) or default

        def number(name: str, default: float) -> float:
            raw = pick(name)
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        api_token = pick("API_TOKEN")
        if not api_token:
            # Local development convenience only; production must set it.
            api_token = "dev-local-token"
        return cls(
            database_url=pick("DATABASE_URL", DEFAULT_DATABASE_URL),
            api_token=api_token,
            demo_token=pick("DEMO_TOKEN"),
            workers=int(number("API_WORKERS", 6)),
            queue_size=int(number("API_QUEUE_SIZE", 50)),
            per_ip_per_minute=int(number("API_RATE_PER_MIN", 5)),
            per_ip_per_day=int(number("API_RATE_PER_DAY", 30)),
            demo_per_day=int(number("API_DEMO_PER_DAY", 5)),
            daily_budget_usd=number("API_DAILY_BUDGET_USD", 5.0),
            run_timeout_s=number("API_RUN_TIMEOUT_S", 120.0),
            review_mode=pick("API_REVIEW_MODE", "1") == "1",
            trust_proxy=pick("TRUST_PROXY", "0") == "1",
            langfuse_public_key=pick("LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=pick("LANGFUSE_SECRET_KEY"),
            langfuse_host=pick("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
