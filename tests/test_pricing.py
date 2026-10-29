from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agent.pricing import PricingError, load_pricing

SHANGHAI = ZoneInfo("Asia/Shanghai")

CONFIG = """
version: "test-1"
currency: USD
timezone: Asia/Shanghai
peak_windows:
  - "09:00-12:00"
  - "14:00-18:00"
off_peak_multiplier: 0.5
models:
  deepseek-flash:
    input_hit_per_mtok: 0.006
    input_miss_per_mtok: 0.3
    output_per_mtok: 1.2
"""


def _config(tmp_path: Path, body: str = CONFIG) -> Path:
    path = tmp_path / "pricing.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _holidays(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "holidays.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI)


def test_three_price_lines_are_independent(tmp_path: Path) -> None:
    pricing = load_pricing(_config(tmp_path))

    # 2026-10-29 是周四 10:00
    hit = pricing.cost_usd(
        "deepseek-flash",
        tokens_in_hit=1_000_000,
        tokens_in_miss=0,
        tokens_out=0,
        at=_at(2026, 10, 29, 10),
    )
    miss = pricing.cost_usd(
        "deepseek-flash",
        tokens_in_hit=0,
        tokens_in_miss=1_000_000,
        tokens_out=0,
        at=_at(2026, 10, 29, 10),
    )
    out = pricing.cost_usd(
        "deepseek-flash",
        tokens_in_hit=0,
        tokens_in_miss=0,
        tokens_out=1_000_000,
        at=_at(2026, 10, 29, 10),
    )

    assert hit == pytest.approx(0.006)
    assert miss == pytest.approx(0.3)
    assert out == pytest.approx(1.2)
    # 命中价不是未命中价的一半（旧规则曾误以为如此）
    assert hit != pytest.approx(miss / 2)


def test_off_peak_halves_every_line(tmp_path: Path) -> None:
    pricing = load_pricing(_config(tmp_path))

    # 2026-10-31 是周六 10:00
    off = pricing.cost_usd(
        "deepseek-flash",
        tokens_in_hit=1_000_000,
        tokens_in_miss=1_000_000,
        tokens_out=1_000_000,
        at=_at(2026, 10, 31, 10),
    )

    # (0.006 + 0.3 + 1.2) * 0.5
    assert off == pytest.approx(0.753)


def test_peak_window_boundaries(tmp_path: Path) -> None:
    pricing = load_pricing(_config(tmp_path))

    assert pricing.is_peak(_at(2026, 10, 29, 8, 59)) is False
    assert pricing.is_peak(_at(2026, 10, 29, 9, 0)) is True
    assert pricing.is_peak(_at(2026, 10, 29, 11, 59)) is True
    assert pricing.is_peak(_at(2026, 10, 29, 12, 0)) is False
    assert pricing.is_peak(_at(2026, 10, 29, 13, 59)) is False
    assert pricing.is_peak(_at(2026, 10, 29, 14, 0)) is True
    assert pricing.is_peak(_at(2026, 10, 29, 17, 59)) is True
    assert pricing.is_peak(_at(2026, 10, 29, 18, 0)) is False


def test_weekend_is_off_peak(tmp_path: Path) -> None:
    pricing = load_pricing(_config(tmp_path))

    assert pricing.is_peak(_at(2026, 10, 31, 10)) is False


def test_statutory_holiday_is_off_peak(tmp_path: Path) -> None:
    pricing = load_pricing(_config(tmp_path), _holidays(tmp_path, '2026:\n  - "2026-10-01"\n'))

    # 2026-10-01 是周四
    assert pricing.is_peak(_at(2026, 10, 1, 10)) is False


def test_holidays_flag_reflects_configuration(tmp_path: Path) -> None:
    # 显式传入空表，避免依赖仓库里那份真实节假日文件
    pricing = load_pricing(_config(tmp_path), _holidays(tmp_path, "2026: []\n"))

    assert pricing.holidays_configured is False


def test_naive_datetime_is_rejected(tmp_path: Path) -> None:
    pricing = load_pricing(_config(tmp_path))

    with pytest.raises(ValueError, match="timezone-aware"):
        pricing.is_peak(datetime(2026, 10, 29, 10))


def test_missing_model_lists_known_models(tmp_path: Path) -> None:
    pricing = load_pricing(_config(tmp_path))

    with pytest.raises(PricingError, match="deepseek-chat"):
        pricing.price_for("deepseek-chat")


def test_zero_price_is_rejected(tmp_path: Path) -> None:
    body = CONFIG.replace("input_miss_per_mtok: 0.3", "input_miss_per_mtok: 0")

    with pytest.raises(PricingError, match="budget guard"):
        load_pricing(_config(tmp_path, body))


def test_missing_price_field_is_rejected(tmp_path: Path) -> None:
    body = CONFIG.replace("    input_hit_per_mtok: 0.006\n", "")

    with pytest.raises(PricingError, match="input_hit_per_mtok"):
        load_pricing(_config(tmp_path, body))


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PricingError, match="not found"):
        load_pricing(tmp_path / "nope.yaml")
