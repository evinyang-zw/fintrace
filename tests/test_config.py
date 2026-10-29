from __future__ import annotations

from pathlib import Path

import pytest

from pipelines.config import Settings


def test_env_file_is_used_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("LLM_MODEL=from-dotenv\nLLM_API_KEY=dotenv-key\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    settings = Settings.from_env()

    assert settings.llm_model == "from-dotenv"
    assert settings.llm_api_key == "dotenv-key"


def test_shell_env_wins_over_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("LLM_MODEL=from-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_MODEL", "from-env")

    assert Settings.from_env().llm_model == "from-env"


def test_deepseek_key_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-alias")

    assert Settings.from_env().llm_api_key == "sk-alias"
