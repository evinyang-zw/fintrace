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
# W3: one vendor for both embedding and rerank (Aliyun Bailian / DashScope).
# Native DashScope endpoints (Bailian workspace endpoints look like this too).
# A URL containing "/api/v1/services/" selects the native payload; anything else
# is treated as OpenAI-compatible and must end with "/embeddings".
DEFAULT_EMBEDDING_BASE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
)
DEFAULT_EMBEDDING_MODEL = "text-embedding-v3"
DEFAULT_EMBEDDING_DIM = 1024
DEFAULT_RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
DEFAULT_RERANK_MODEL = "gte-rerank-v2"
# bm25 | dense | hybrid | hybrid_rerank
DEFAULT_RETRIEVAL_TIER = "bm25"


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


def _as_int(name: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


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
    # W3 retrieval stack. Empty key means "degrade to BM25 and say so".
    embedding_base_url: str = DEFAULT_EMBEDDING_BASE_URL
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dim: int = DEFAULT_EMBEDDING_DIM
    embedding_api_key: str = ""
    rerank_url: str = DEFAULT_RERANK_URL
    rerank_model: str = DEFAULT_RERANK_MODEL
    rerank_api_key: str = ""
    retrieval_tier: str = DEFAULT_RETRIEVAL_TIER

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
            embedding_base_url=pick("EMBEDDING_BASE_URL", DEFAULT_EMBEDDING_BASE_URL),
            embedding_model=pick("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            embedding_dim=_as_int(
                "EMBEDDING_DIM", pick("EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM))
            ),
            embedding_api_key=pick("DASHSCOPE_API_KEY") or pick("EMBEDDING_API_KEY"),
            rerank_url=pick("RERANK_URL", DEFAULT_RERANK_URL),
            rerank_model=pick("RERANK_MODEL", DEFAULT_RERANK_MODEL),
            rerank_api_key=pick("DASHSCOPE_API_KEY") or pick("RERANK_API_KEY"),
            retrieval_tier=pick("RETRIEVAL_TIER", DEFAULT_RETRIEVAL_TIER),
        )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def report_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def has_embeddings(self) -> bool:
        return bool(self.embedding_api_key)

    @property
    def resolved_rerank_key(self) -> str:
        return self.rerank_api_key or self.embedding_api_key
