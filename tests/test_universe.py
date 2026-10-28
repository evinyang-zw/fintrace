from __future__ import annotations

from pathlib import Path

import pytest

import pipelines.universe as universe
from pipelines.sources.base import CompanyRef

YAML = """\
source: edgar
since: 2021-01-01
tickers:
  - {ticker: MSFT, group: cloud}
  - {ticker: KO, group: beverage}
"""


def test_load_universe_reads_entries_and_since(tmp_path: Path) -> None:
    path = tmp_path / "universe.yaml"
    path.write_text(YAML, encoding="utf-8")
    entries, since = universe.load_universe(path)
    assert [e["ticker"] for e in entries] == ["MSFT", "KO"]
    assert entries[0]["group"] == "cloud"
    assert since == "2021-01-01"


def test_lock_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = tmp_path / "universe.lock.json"
    monkeypatch.setattr(universe, "LOCK_PATH", lock)
    companies = [
        CompanyRef(cik="0000789019", ticker="MSFT", name="Microsoft", group="cloud"),
        CompanyRef(cik="0000021344", ticker="KO", name="Coca-Cola", group="beverage"),
    ]
    universe.write_lock(companies)
    assert universe.load_lock() == companies
