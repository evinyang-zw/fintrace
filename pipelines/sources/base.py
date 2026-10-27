"""The only interface an ingestion source has to satisfy.

Deliberately contains no EDGAR vocabulary: an A-share adapter (W6 stretch)
only needs to map its documents onto these four calls.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CompanyRef:
    cik: str
    ticker: str
    name: str
    group: str | None = None


@dataclass(frozen=True, slots=True)
class FilingRef:
    accession: str
    cik: str
    form: str
    filed_at: date
    period_end: date | None = None
    primary_doc: str = ""


@dataclass(frozen=True, slots=True)
class RawDoc:
    url: str
    path: Path
    sha256: str
    html: str


@dataclass(frozen=True, slots=True)
class Fact:
    cik: str
    tag: str
    unit: str
    period_start: date
    period_end: date
    value: float
    filed_at: date
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    form: str | None = None
    accn: str | None = None
    frame: str | None = None


@runtime_checkable
class DataSource(Protocol):
    name: str
    filing_forms: frozenset[str]

    def list_companies(self, tickers: Sequence[str]) -> list[CompanyRef]: ...

    def list_filings(self, company: CompanyRef, since: date, until: date) -> list[FilingRef]: ...

    def fetch_document(self, filing: FilingRef) -> RawDoc: ...

    def fetch_facts(self, company: CompanyRef, since: date) -> list[Fact]: ...
