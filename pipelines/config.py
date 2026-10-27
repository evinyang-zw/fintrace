"""Runtime configuration. Everything overridable by environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATABASE_URL = "postgresql://fintrace:fintrace@localhost:5433/fintrace"
DEFAULT_USER_AGENT = "FinTrace research contact@example.com"


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_url: str
    sec_user_agent: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=Path(os.environ.get("FINTRACE_DATA_DIR", "data")),
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            sec_user_agent=os.environ.get("SEC_USER_AGENT", DEFAULT_USER_AGENT),
        )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def report_dir(self) -> Path:
        return self.data_dir / "reports"
