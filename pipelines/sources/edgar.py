"""SEC EDGAR source: submissions, company facts, primary documents."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from pipelines.sources.base import CompanyRef, Fact, FilingRef, RawDoc
from pipelines.sources.http import Fetcher, sha256_text

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{doc}"

DEFAULT_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})

_FILING_FIELDS = (
    "accessionNumber",
    "filingDate",
    "reportDate",
    "form",
    "primaryDocument",
)


class EdgarSource:
    """EDGAR adapter. Rate limit is handled by the shared :class:`Fetcher`."""

    name = "edgar"
    filing_forms = DEFAULT_FORMS

    def __init__(self, fetcher: Fetcher, raw_dir: Path) -> None:
        self._fetcher = fetcher
        self._raw_dir = raw_dir / "edgar"

    # -- discovery ---------------------------------------------------------

    def list_companies(self, tickers: Sequence[str]) -> list[CompanyRef]:
        payload: dict[str, dict[str, Any]] = self._fetcher.get_json(
            TICKERS_URL, self._raw_dir / "_company_tickers.json"
        )
        wanted = {t.upper() for t in tickers}
        found: dict[str, CompanyRef] = {}
        for row in payload.values():
            ticker = str(row["ticker"]).upper()
            if ticker in wanted:
                found[ticker] = CompanyRef(
                    cik=str(row["cik_str"]).zfill(10),
                    ticker=ticker,
                    name=str(row["title"]),
                )
        missing = wanted - found.keys()
        if missing:
            raise ValueError(f"tickers not found on EDGAR: {sorted(missing)}")
        return [found[t] for t in tickers]

    def list_filings(self, company: CompanyRef, since: date, until: date) -> list[FilingRef]:
        submissions = self._submissions(company.cik)
        rows = list(self._filing_rows(company.cik, submissions))
        filings: list[FilingRef] = []
        for row in rows:
            form = str(row.get("form", ""))
            if form not in self.filing_forms:
                continue
            filed_at = _parse_date(row.get("filingDate"))
            if filed_at is None or filed_at < since or filed_at > until:
                continue
            filings.append(
                FilingRef(
                    accession=str(row["accessionNumber"]),
                    cik=company.cik,
                    form=form,
                    filed_at=filed_at,
                    period_end=_parse_date(row.get("reportDate")),
                    primary_doc=str(row.get("primaryDocument", "")),
                )
            )
        filings.sort(key=lambda f: (f.filed_at, f.accession))
        return filings

    # -- documents ---------------------------------------------------------

    def fetch_document(self, filing: FilingRef) -> RawDoc:
        if not filing.primary_doc:
            raise ValueError(f"filing {filing.accession} has no primary document")
        url = ARCHIVE_URL.format(
            cik_int=int(filing.cik),
            accession=filing.accession.replace("-", ""),
            doc=filing.primary_doc,
        )
        cache_path = self._raw_dir / filing.cik / f"{filing.accession}.html"
        html = self._fetcher.get_text(url, cache_path)
        return RawDoc(url=url, path=cache_path, sha256=sha256_text(html), html=html)

    # -- facts -------------------------------------------------------------

    def fetch_facts(self, company: CompanyRef, since: date) -> list[Fact]:
        payload: dict[str, Any] = self._fetcher.get_json(
            COMPANY_FACTS_URL.format(cik=company.cik),
            self._raw_dir / company.cik / "companyfacts.json",
        )
        return list(_iter_facts(company.cik, payload, since))

    # -- internals ---------------------------------------------------------

    def _submissions(self, cik: str) -> dict[str, Any]:
        payload: dict[str, Any] = self._fetcher.get_json(
            SUBMISSIONS_URL.format(cik=cik),
            self._raw_dir / cik / "submissions.json",
        )
        return payload

    def _filing_rows(self, cik: str, submissions: dict[str, Any]) -> Iterable[dict[str, Any]]:
        """EDGAR stores filings column-wise; older ones spill into ``filings.files``."""
        yield from _columnar_rows(submissions["filings"]["recent"])
        for extra in submissions["filings"].get("files", []):
            name = str(extra["name"])
            payload: dict[str, Any] = self._fetcher.get_json(
                f"https://data.sec.gov/submissions/{name}",
                self._raw_dir / cik / name,
            )
            yield from _columnar_rows(payload)


def _columnar_rows(block: dict[str, list[Any]]) -> Iterable[dict[str, Any]]:
    if not block.get("accessionNumber"):
        return
    length = len(block["accessionNumber"])
    for i in range(length):
        yield {key: block[key][i] for key in _FILING_FIELDS if key in block}


def _iter_facts(cik: str, payload: dict[str, Any], since: date) -> Iterable[Fact]:
    for _taxonomy, tags in payload.get("facts", {}).items():
        for tag, body in tags.items():
            for unit, entries in body.get("units", {}).items():
                for entry in entries:
                    fact = _parse_fact(cik, tag, unit, entry, since)
                    if fact is not None:
                        yield fact


def _parse_fact(cik: str, tag: str, unit: str, entry: dict[str, Any], since: date) -> Fact | None:
    end = _parse_date(entry.get("end"))
    filed = _parse_date(entry.get("filed"))
    value = entry.get("val")
    if end is None or filed is None or value is None:
        return None
    if end < since:
        return None
    if entry.get("form") not in DEFAULT_FORMS:
        return None
    return Fact(
        cik=cik,
        tag=tag,
        unit=unit,
        period_start=_parse_date(entry.get("start")) or date(1900, 1, 1),
        period_end=end,
        value=float(value),
        filed_at=filed,
        fiscal_year=_as_int(entry.get("fy")),
        fiscal_period=entry.get("fp"),
        form=entry.get("form"),
        accn=entry.get("accn"),
        frame=entry.get("frame"),
    )


def _parse_date(raw: object) -> date | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _as_int(raw: object) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None
