"""Token pricing under DeepSeek's peak / off-peak rules.

Official rules (pricing page fetched 2026-10-29):
* three independent prices per model — cached input, uncached input, output;
  the cached price is NOT derived from the uncached one;
* off-peak is half of peak for every one of them;
* peak is 01:00-04:00 and 06:00-10:00 UTC on Mon-Fri, excluding Chinese public
  holidays; that is 09:00-12:00 and 14:00-18:00 Asia/Shanghai.

Cost is computed from the *served* model and the server-side creation time —
never from the requested alias or the local clock. The project prices
everything in USD, hence ``cost_usd``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DEFAULT_PRICING_PATH = Path("config/pricing.yaml")
DEFAULT_HOLIDAYS_PATH = Path("config/holidays.yaml")
DEFAULT_TIMEZONE = "Asia/Shanghai"
TOKENS_PER_MTOK = 1_000_000


class PricingError(RuntimeError):
    """Raised when a price or a pricing rule is missing or unusable."""


@dataclass(frozen=True, slots=True)
class Price:
    """Peak-hour base prices, in USD per million tokens."""

    input_hit_per_mtok: float
    input_miss_per_mtok: float
    output_per_mtok: float


@dataclass(frozen=True, slots=True)
class Pricing:
    version: str
    currency: str
    timezone: str
    peak_windows: tuple[tuple[time, time], ...]
    off_peak_multiplier: float
    holidays: frozenset[date]
    holidays_configured: bool
    models: dict[str, Price]

    def price_for(self, model: str) -> Price:
        try:
            return self.models[model]
        except KeyError as exc:
            known = ", ".join(sorted(self.models)) or "<empty>"
            raise PricingError(
                f"no price for served model {model!r}; known models: {known}. "
                "Add it to config/pricing.yaml — keys must match the served model."
            ) from exc

    def is_peak(self, at: datetime) -> bool:
        """Peak = Mon-Fri, inside a peak window, and not a statutory holiday."""
        if at.tzinfo is None:
            raise ValueError("at must be timezone-aware")
        local = at.astimezone(ZoneInfo(self.timezone))
        if local.weekday() >= 5 or local.date() in self.holidays:
            return False
        clock = local.time()
        return any(start <= clock < end for start, end in self.peak_windows)

    def cost_usd(
        self,
        model: str,
        *,
        tokens_in_hit: int,
        tokens_in_miss: int,
        tokens_out: int,
        at: datetime,
    ) -> float:
        price = self.price_for(model)
        subtotal = (
            tokens_in_hit * price.input_hit_per_mtok
            + tokens_in_miss * price.input_miss_per_mtok
            + tokens_out * price.output_per_mtok
        ) / TOKENS_PER_MTOK
        return subtotal if self.is_peak(at) else subtotal * self.off_peak_multiplier


def _positive(value: Any, *, model: str, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PricingError(f"{model}.{field} must be a number, got {value!r}")
    number = float(value)
    if number <= 0:
        raise PricingError(
            f"{model}.{field} must be > 0 (currently {value!r}); "
            "a zero price silently disables the budget guard."
        )
    return number


def _multiplier(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PricingError(f"{name} must be a number, got {value!r}")
    number = float(value)
    if not 0 < number <= 1:
        raise PricingError(f"{name} must be in (0, 1], got {value!r}")
    return number


def _parse_windows(raw: Any) -> tuple[tuple[time, time], ...]:
    if raw is None:
        raise PricingError("peak_windows is required (use [] if there is no peak pricing)")
    if not isinstance(raw, list):
        raise PricingError("peak_windows must be a list of 'HH:MM-HH:MM' strings")
    windows: list[tuple[time, time]] = []
    for item in raw:
        if not isinstance(item, str) or item.count("-") != 1:
            raise PricingError(f"bad peak window: {item!r}")
        start_raw, end_raw = item.split("-")
        start = time.fromisoformat(start_raw.strip())
        end = time.fromisoformat(end_raw.strip())
        if start >= end:
            raise PricingError(f"peak window {item!r} must start before it ends")
        windows.append((start, end))
    return tuple(windows)


def _load_holidays(path: Path) -> frozenset[date]:
    if not path.is_file():
        return frozenset()
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise PricingError(f"{path}: expected a mapping of year -> list of dates")
    days: set[date] = set()
    for year, items in raw.items():
        if not isinstance(items, list):
            raise PricingError(f"{path}: {year!r} must be a list of ISO dates")
        for item in items:
            if isinstance(item, datetime):
                days.add(item.date())
            elif isinstance(item, date):
                days.add(item)
            else:
                try:
                    days.add(date.fromisoformat(str(item)))
                except ValueError as exc:
                    raise PricingError(f"{path}: bad date {item!r}") from exc
    return frozenset(days)


def load_pricing(
    path: Path = DEFAULT_PRICING_PATH,
    holidays_path: Path = DEFAULT_HOLIDAYS_PATH,
) -> Pricing:
    if not path.is_file():
        raise PricingError(f"pricing file not found: {path}")

    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PricingError(f"{path}: top level must be a mapping")

    tz_name = str(raw.get("timezone", DEFAULT_TIMEZONE))
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise PricingError(f"{path}: unknown timezone {tz_name!r}") from exc

    models_raw = raw.get("models")
    if not isinstance(models_raw, dict) or not models_raw:
        raise PricingError(f"{path}: `models` must be a non-empty mapping")

    models: dict[str, Price] = {}
    for name, spec in models_raw.items():
        if not isinstance(spec, dict):
            raise PricingError(f"{path}: model {name!r} must be a mapping")
        models[str(name)] = Price(
            input_hit_per_mtok=_positive(
                spec.get("input_hit_per_mtok"), model=str(name), field="input_hit_per_mtok"
            ),
            input_miss_per_mtok=_positive(
                spec.get("input_miss_per_mtok"), model=str(name), field="input_miss_per_mtok"
            ),
            output_per_mtok=_positive(
                spec.get("output_per_mtok"), model=str(name), field="output_per_mtok"
            ),
        )

    holidays = _load_holidays(holidays_path)
    return Pricing(
        version=str(raw.get("version", "unversioned")),
        currency=str(raw.get("currency", "USD")),
        timezone=tz_name,
        peak_windows=_parse_windows(raw.get("peak_windows")),
        off_peak_multiplier=_multiplier(
            raw.get("off_peak_multiplier", 0.5), name="off_peak_multiplier"
        ),
        holidays=holidays,
        holidays_configured=bool(holidays),
        models=models,
    )


def _main() -> None:
    pricing = load_pricing()
    print(f"version={pricing.version} currency={pricing.currency} tz={pricing.timezone}")
    windows = ", ".join(f"{start:%H:%M}-{end:%H:%M}" for start, end in pricing.peak_windows)
    print(f"peak: {windows or 'none'} (Mon-Fri, holidays excluded)")
    print(f"off-peak x{pricing.off_peak_multiplier:g}")
    if pricing.holidays_configured:
        years = sorted({day.year for day in pricing.holidays})
        print(f"holidays: {len(pricing.holidays)} 天，覆盖 {years}")
    else:
        print("warn: 节假日未配置——节假日的工作日会按高峰计价（成本偏高）")

    peak_at = datetime(2026, 10, 29, 10, 0, tzinfo=ZoneInfo(pricing.timezone))
    off_at = datetime(2026, 10, 31, 10, 0, tzinfo=ZoneInfo(pricing.timezone))
    for model in sorted(pricing.models):
        peak = pricing.cost_usd(
            model, tokens_in_hit=0, tokens_in_miss=1_000_000, tokens_out=500_000, at=peak_at
        )
        off = pricing.cost_usd(
            model, tokens_in_hit=0, tokens_in_miss=1_000_000, tokens_out=500_000, at=off_at
        )
        hit = pricing.cost_usd(
            model, tokens_in_hit=1_000_000, tokens_in_miss=0, tokens_out=0, at=peak_at
        )
        print(
            f"  {model}: 高峰 1M未命中+0.5M输出 = ${peak:.4f}"
            f" | 空闲同量 = ${off:.4f}"
            f" | 高峰 1M命中 = ${hit:.4f}"
        )


if __name__ == "__main__":
    _main()
