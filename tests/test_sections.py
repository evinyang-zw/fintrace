from __future__ import annotations

import itertools

from pipelines.sections import split_sections

TEN_K = """TABLE OF CONTENTS
Item 1. Business .......... 3
Item 1A. Risk Factors .......... 12
Item 7. Management's Discussion .......... 25
Item 8. Financial Statements .......... 40

PART I
Item 1. Business
We design, manufacture and sell consumer devices. This section is intentionally long
so that the section is not discarded by the minimum-length rule. Lorem ipsum dolor
sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore.

Item 1A. Risk Factors
Our business faces many risks. This paragraph is also long enough to survive the
minimum length check, because real risk factor sections are pages long. The market
is competitive and margins may decline.

Item 2. Properties
We own facilities in several countries.

PART II
Item 7. Management's Discussion and Analysis of Financial Condition
Revenue increased year over year driven by services growth. This is a long enough
paragraph for the section splitter to keep it in the output.

Item 7A. Quantitative and Qualitative Disclosures About Market Risk
We are exposed to interest rate risk.

Item 8. Financial Statements and Supplementary Data
The consolidated financial statements are included herein.
"""

TEN_Q = """TABLE OF CONTENTS

PART I
Item 1. Financial Statements
Condensed consolidated balance sheets are presented below in this section, followed
by statements of operations and cash flows for the period.

Item 2. Management's Discussion and Analysis
Revenue grew modestly this quarter on the back of strong demand.

PART II
Item 1. Legal Proceedings
We are party to various routine legal proceedings.

Item 1A. Risk Factors
There have been no material changes to the risk factors described previously.
"""


def test_ten_k_sections_skip_toc() -> None:
    sections = split_sections(TEN_K, "10-K")
    codes = [s.item_code for s in sections]
    assert codes[0] == "1"
    assert "1A" in codes
    assert "7" in codes
    assert "8" in codes
    assert codes.index("1") < codes.index("7")


def test_sections_are_contiguous_and_ordered() -> None:
    sections = split_sections(TEN_K, "10-K")
    for current, following in itertools.pairwise(sections):
        assert current.char_start < current.char_end
        assert current.char_end == following.char_start


def test_section_text_matches_offsets() -> None:
    sections = split_sections(TEN_K, "10-K")
    for section in sections:
        assert section.text == TEN_K[section.char_start : section.char_end]


def test_ten_q_uses_part_qualified_codes() -> None:
    sections = split_sections(TEN_Q, "10-Q")
    codes = [s.item_code for s in sections]
    assert "I-1" in codes
    assert "II-1" in codes
    assert "II-1A" in codes


def test_fallback_to_full_document() -> None:
    sections = split_sections("This document has no item headings at all.", "10-K")
    assert len(sections) == 1
    assert sections[0].item_code == "FULL"
    assert sections[0].char_start == 0
    assert sections[0].char_end == len("This document has no item headings at all.")


RUNNING_HEADERS = """PART I
Item 1
Our Business at a Glance
3
TABLE OF CONTENTS
PART I
Item 1.
Business
1
Item 1A.
Risk Factors
9
PART II
Item 7.
Management's Discussion
20
Item 8.
Financial Statements
30

PART I
Item 1
Our Business at a Glance
5
ITEM 1. BUSINESS
Microsoft is a technology company. This paragraph is long enough to represent the
actual business section, which in a real filing runs for dozens of pages of prose.

PART II
Item 7.
Management's Discussion and Analysis
Revenue increased year over year. This section is also long enough to be kept.

Item 8.
Financial Statements and Supplementary Data
The consolidated financial statements are included herein by reference.
"""


def test_running_page_headers_are_not_sections() -> None:
    sections = split_sections(RUNNING_HEADERS, "10-K")
    assert sections[0].item_code == "1"
    assert sections[0].text.startswith("ITEM 1. BUSINESS")
    assert {s.item_code for s in sections} >= {"1", "7", "8"}


def test_em_dash_headings() -> None:
    text = """PART I\u2014FINANCIAL INFORMATION
Item 1\u2014Financial Statements
Condensed consolidated balance sheets are presented below in this section.

Item 2\u2014Management's Discussion and Analysis
Revenue grew modestly this quarter on the back of strong demand.

PART II\u2014OTHER INFORMATION
Item 1\u2014Legal Proceedings
We are party to various routine legal proceedings.
"""
    sections = split_sections(text, "10-Q")
    assert [s.item_code for s in sections][:2] == ["I-1", "I-2"]


def test_partial_headers_fall_back_to_full() -> None:
    text = "Item 1\u2014Financial Statements\nshort amendment with a single item"
    sections = split_sections(text, "10-Q/A")
    assert sections[0].item_code == "FULL"
