from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

from pipelines.sources.edgar import EdgarSource, _columnar_rows, _parse_fact
from pipelines.sources.http import Fetcher

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}

SUBMISSIONS = {
    "filings": {
        "recent": {
            "accessionNumber": [
                "0000320193-24-000123",
                "0000320193-24-000045",
                "0000320193-24-000999",
            ],
            "filingDate": ["2024-11-01", "2024-08-02", "2020-01-01"],
            "reportDate": ["2024-09-28", "2024-06-29", ""],
            "form": ["10-K", "10-Q", "8-K"],
            "primaryDocument": ["aapl-20240928.htm", "aapl-20240629.htm", "x.htm"],
        },
        "files": [],
    }
}


def _source(tmp_path: Path, routes: dict[str, httpx.Response]) -> EdgarSource:
    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, response in routes.items():
            if fragment in str(request.url):
                return response
        return httpx.Response(404)

    fetcher = Fetcher(
        tmp_path, "test-agent", min_interval_s=0, transport=httpx.MockTransport(handler)
    )
    return EdgarSource(fetcher, tmp_path)


def test_list_companies_pads_cik(tmp_path: Path) -> None:
    source = _source(tmp_path, {"company_tickers.json": httpx.Response(200, json=TICKERS)})
    companies = source.list_companies(["AAPL", "MSFT"])
    assert [c.ticker for c in companies] == ["AAPL", "MSFT"]
    assert companies[0].cik == "0000320193"


def test_list_companies_rejects_unknown_ticker(tmp_path: Path) -> None:
    source = _source(tmp_path, {"company_tickers.json": httpx.Response(200, json=TICKERS)})
    try:
        source.list_companies(["NOPE"])
    except ValueError as exc:
        assert "NOPE" in str(exc)
        return
    raise AssertionError("expected ValueError")


def test_list_filings_filters_form_and_date(tmp_path: Path) -> None:
    source = _source(
        tmp_path, {"submissions/CIK0000320193.json": httpx.Response(200, json=SUBMISSIONS)}
    )
    from pipelines.sources.base import CompanyRef

    company = CompanyRef(cik="0000320193", ticker="AAPL", name="Apple Inc.")
    filings = source.list_filings(company, date(2021, 1, 1), date(2025, 1, 1))
    assert [f.form for f in filings] == ["10-Q", "10-K"]
    assert filings[0].period_end == date(2024, 6, 29)


def test_fetch_document_builds_archive_url(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        {
            "Archives/edgar/data/320193/000032019324000123/aapl.htm": httpx.Response(
                200, text="<html>filing</html>"
            )
        },
    )
    from pipelines.sources.base import FilingRef

    filing = FilingRef(
        doc_id="0000320193-24-000123",
        cik="0000320193",
        form="10-K",
        filed_at=date(2024, 11, 1),
        primary_doc="aapl.htm",
    )
    doc = source.fetch_document(filing)
    assert doc.html == "<html>filing</html>"
    assert doc.sha256
    assert doc.path.exists()


def test_columnar_rows() -> None:
    rows = list(_columnar_rows(SUBMISSIONS["filings"]["recent"]))
    assert len(rows) == 3
    assert rows[0]["form"] == "10-K"


def test_parse_fact_drops_old_periods_and_other_forms() -> None:
    entry = {
        "start": "2023-07-01",
        "end": "2024-06-30",
        "val": 245122000000,
        "filed": "2024-07-30",
        "fy": 2024,
        "fp": "FY",
        "form": "10-K",
        "accn": "0000789019-24-000001",
    }
    fact = _parse_fact("0000789019", "Revenues", "USD", entry, date(2021, 1, 1))
    assert fact is not None
    assert fact.fiscal_year == 2024
    assert fact.period_start == date(2023, 7, 1)
    assert _parse_fact("x", "Revenues", "USD", entry, date(2025, 1, 1)) is None
    other_form = dict(entry, form="8-K")
    assert _parse_fact("x", "Revenues", "USD", other_form, date(2021, 1, 1)) is None
