"""Runtime configuration. Everything overridable by environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_DATABASE_URL = "postgresql://fintrace:fintrace@localhost:5433/fintrace"
DEFAULT_USER_AGENT = "FinTrace research contact@example.com"
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_LLM_MODEL = "deepseek-flash"


def _dotenv() -> dict[str, str]:
    """Read .env without mutating os.environ. Missing file means an empty mapping."""
    path = Path(".env")
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for key, value in dotenv_values(path).items():
        if value is not None:
            values[key] = value
    return values


def _as_float(name: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_url: str
    sec_user_agent: str
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_model: str = DEFAULT_LLM_MODEL
    llm_api_key: str = ""
    llm_temperature: float = 0.0
    llm_thinking: str = "disabled"

    @classmethod
    def from_env(cls) -> Settings:
        # Precedence: real environment > .env > code default. .env is gitignored,
        # so CI and production never depend on a file that is not in the repo.
        env_file = _dotenv()

        def pick(name: str, default: str = "") -> str:
            return os.environ.get(name) or env_file.get(name) or default

        return cls(
            data_dir=Path(pick("FINTRACE_DATA_DIR", "data")),
            database_url=pick("DATABASE_URL", DEFAULT_DATABASE_URL),
            sec_user_agent=pick("SEC_USER_AGENT", DEFAULT_USER_AGENT),
            llm_base_url=pick("LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
            llm_model=pick("LLM_MODEL", DEFAULT_LLM_MODEL),
            # LLM_API_KEY is the provider-agnostic name; DEEPSEEK_API_KEY is kept
            # as an alias so the DeepSeek-specific name also works.
            llm_api_key=pick("LLM_API_KEY") or pick("DEEPSEEK_API_KEY"),
            llm_temperature=_as_float("LLM_TEMPERATURE", pick("LLM_TEMPERATURE", "0")),
            llm_thinking=pick("LLM_THINKING", "disabled"),
        )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def report_dir(self) -> Path:
        return self.data_dir / "reports"
