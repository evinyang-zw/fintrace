"""Split a filing's plain text into 10-K / 10-Q items.

Three-level strategy required by the W1 plan:
  1. regex + sequence selection (this module)
  2. per-company override file (``config/sections/{ticker}.yaml``) - later
  3. fall back to a single ``FULL`` section, marked for review

Two things make real 10-Ks harder than the toy example:

* Every page carries a running header repeating "Item 1" and "Table of Contents".
  So the heading regex requires punctuation after the number, and the TOC is
  detected as a *dense cluster* of headings near the top, not by searching for a
  literal string.
* Headings differ per filer: ``Item 1.``, ``Item 1—``, ``PART II—OTHER
  INFORMATION``. The regexes below cover those variants; anything still missed
  should become a per-company override, not a cleverer universal parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ITEM_ORDER_10K = [
    "1",
    "1A",
    "1B",
    "1C",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "7A",
    "8",
    "9",
    "9A",
    "9B",
    "9C",
    "10",
    "11",
    "12",
    "13",
    "14",
    "15",
]
_ITEM_ORDER_10Q = {
    "I": ["1", "2", "3", "4"],
    "II": ["1", "1A", "2", "3", "4", "5", "6"],
}

# Punctuation after the number is mandatory: running page headers write "Item 1"
# with no punctuation and would otherwise produce ~100 bogus candidates.
_HEADING_RE = re.compile(
    r"^[ \t]*item[ \t]+(\d{1,2}[A-Ca-c]?)[ \t]*[.:\u2014\u2013-][ \t]*(.{0,160})$",
    re.MULTILINE | re.IGNORECASE,
)
_PART_RE = re.compile(
    r"^[ \t]*part[ \t]+(iv|iii|ii|i)\b[^\n]{0,80}$",
    re.MULTILINE | re.IGNORECASE,
)
_TOC_LINE_RE = re.compile(r"\.{4,}\s*\d+\s*$")

MIN_SECTIONS = 3
MIN_FIRST_SECTION = 50  # guards against a chain starting inside the TOC


@dataclass(frozen=True, slots=True)
class Section:
    item_code: str
    heading: str
    char_start: int
    char_end: int
    text: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    pos: int
    item: str
    rank: int
    code: str
    heading: str


def split_sections(text: str, form: str) -> list[Section]:
    """Return sections in document order, or a single FULL section on failure."""
    is_10q = form.startswith("10-Q")
    candidates = _candidates(text, is_10q)
    selected = _longest_sequence(candidates)
    # A chain that starts inside the table of contents has a tiny first section.
    # Drop that candidate and re-select instead of shipping a 40-character
    # "Item 1" that swallows the rest of the document.
    while len(selected) >= MIN_SECTIONS and selected[1].pos - selected[0].pos < MIN_FIRST_SECTION:
        candidates = [c for c in candidates if c.pos > selected[0].pos]
        selected = _longest_sequence(candidates)
    if len(selected) < MIN_SECTIONS:
        return [_full_section(text)]
    return [_to_section(text, index, selected) for index in range(len(selected))]


def _to_section(text: str, index: int, selected: list[_Candidate]) -> Section:
    candidate = selected[index]
    end = selected[index + 1].pos if index + 1 < len(selected) else len(text)
    return Section(
        item_code=candidate.code,
        heading=candidate.heading,
        char_start=candidate.pos,
        char_end=end,
        text=text[candidate.pos : end],
    )


def _full_section(text: str) -> Section:
    return Section(
        item_code="FULL",
        heading="FULL DOCUMENT",
        char_start=0,
        char_end=len(text),
        text=text,
    )


def _candidates(text: str, is_10q: bool) -> list[_Candidate]:
    parts = [(m.start(), m.group(1).upper()) for m in _PART_RE.finditer(text)]
    ranks = _rank_table(is_10q)
    out: list[_Candidate] = []
    for match in _HEADING_RE.finditer(text):
        heading = match.group(2).strip()
        if _TOC_LINE_RE.search(heading):
            continue
        item = match.group(1).upper()
        pos = match.start()
        part = _part_at(parts, pos) if is_10q else None
        rank = ranks.get((part, item) if is_10q else (None, item))
        if rank is None:
            continue
        code = f"{part}-{item}" if is_10q else item
        out.append(_Candidate(pos=pos, item=item, rank=rank, code=code, heading=heading[:160]))
    return out


def _rank_table(is_10q: bool) -> dict[tuple[str | None, str], int]:
    if not is_10q:
        return {(None, item): i for i, item in enumerate(_ITEM_ORDER_10K)}
    table: dict[tuple[str | None, str], int] = {}
    rank = 0
    for part, items in _ITEM_ORDER_10Q.items():
        for item in items:
            table[(part, item)] = rank
            rank += 1
    return table


def _part_at(part_positions: list[tuple[int, str]], pos: int) -> str:
    current = "I"
    for start, part in part_positions:
        if start > pos:
            break
        current = part
    return current


def _longest_sequence(candidates: list[_Candidate]) -> list[_Candidate]:
    """Longest strictly-increasing rank chain; ties go to the later start.

    Rank order encodes the canonical item sequence, and requiring it to increase
    is what rejects the table of contents (whose items repeat later in the body).
    Maximising *depth* rather than span is what keeps every item in the chain;
    the tie-break on a later start is what prefers body headings over TOC
    entries when both cover the same items.
    """
    if not candidates:
        return []
    n = len(candidates)
    depth = [1] * n
    first = [c.pos for c in candidates]
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if candidates[j].rank >= candidates[i].rank:
                continue
            if (depth[j] + 1, first[j]) > (depth[i], first[i]):
                depth[i] = depth[j] + 1
                first[i] = first[j]
                prev[i] = j
    best = max(range(n), key=lambda i: (depth[i], first[i]))
    chain: list[_Candidate] = []
    while best != -1:
        chain.append(candidates[best])
        best = prev[best]
    return list(reversed(chain))
