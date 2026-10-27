"""Split a filing's plain text into 10-K / 10-Q items.

Three-level strategy required by the W1 plan:
  1. regex + sequence selection (this module)
  2. per-company override file (``config/sections/{ticker}.yaml``) - later
  3. fall back to a single ``FULL`` section, marked for review

The sequence selector matters: a 10-K contains its item list twice (table of
contents, then the body). We pick the candidate sequence with the largest
character span, which is the body, because the TOC is a few pages long.
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

_HEADING_RE = re.compile(
    r"^[ \t]*item[ \t]+(\d{1,2}[A-Ca-c]?)[ \t]*[.:]?[ \t]*(.{0,160})$",
    re.MULTILINE | re.IGNORECASE,
)
_PART_RE = re.compile(
    r"^[ \t]*part[ \t]+(iv|iii|ii|i)[ \t]*\.?[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
_TOC_HINT_RE = re.compile(r"table\s+of\s+contents", re.IGNORECASE)
_TOC_LINE_RE = re.compile(r"\.{4,}\s*\d+\s*$")

MIN_SECTIONS = 3
# (unused) sections may be short; only the count matters


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
    if len(selected) < MIN_SECTIONS:
        return [_full_section(text)]
    return _materialise(text, selected)


def _full_section(text: str) -> Section:
    return Section(
        item_code="FULL",
        heading="FULL DOCUMENT",
        char_start=0,
        char_end=len(text),
        text=text,
    )


def _body_start(text: str) -> int:
    """Skip the table(s) of contents at the front of the document."""
    limit = int(len(text) * 0.4)
    last = 0
    for match in _TOC_HINT_RE.finditer(text, 0, limit):
        last = match.end()
    return last


def _candidates(text: str, is_10q: bool) -> list[_Candidate]:
    start = _body_start(text)
    part_positions = [(m.start(), m.group(1)) for m in _PART_RE.finditer(text, start)]
    ranks = _rank_table(is_10q)

    out: list[_Candidate] = []
    for match in _HEADING_RE.finditer(text, start):
        item = match.group(1).upper()
        heading = match.group(2).strip()
        if _TOC_LINE_RE.search(heading):
            continue
        part = _part_at(part_positions, match.start()) if is_10q else None
        rank = ranks.get((part, item) if is_10q else (None, item))
        if rank is None:
            continue
        code = f"{part}-{item}" if is_10q else item
        out.append(
            _Candidate(
                pos=match.start(),
                item=item,
                rank=rank,
                code=code,
                heading=heading[:160],
            )
        )
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
    """LIS on rank, maximising the character span covered (body over TOC).

    Span alone is not enough: two chains often cover the same span, and the one
    with fewer items would win a naive tie-break, collapsing five sections into
    "Item 1" + "Item 8". Depth breaks those ties.
    """
    if not candidates:
        return []
    n = len(candidates)
    span = [0] * n
    depth = [1] * n
    first = [c.pos for c in candidates]
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if candidates[j].rank >= candidates[i].rank:
                continue
            candidate_span = candidates[i].pos - first[j]
            candidate_depth = depth[j] + 1
            better = (candidate_span, candidate_depth) > (span[i], depth[i])
            if better:
                span[i] = candidate_span
                depth[i] = candidate_depth
                first[i] = first[j]
                prev[i] = j
    best = max(range(n), key=lambda i: (span[i], depth[i]))
    chain: list[_Candidate] = []
    while best != -1:
        chain.append(candidates[best])
        best = prev[best]
    return list(reversed(chain))


def _materialise(text: str, selected: list[_Candidate]) -> list[Section]:
    """Boundaries are the selected headings; coverage is contiguous by design.

    Deliberately no minimum-length filter: 10-Ks contain one-line items (mine
    safety, exhibit indexes) that are real sections. False positives from the
    table of contents are already handled by sequence selection.
    """
    sections: list[Section] = []
    for index, candidate in enumerate(selected):
        end = selected[index + 1].pos if index + 1 < len(selected) else len(text)
        sections.append(
            Section(
                item_code=candidate.code,
                heading=candidate.heading,
                char_start=candidate.pos,
                char_end=end,
                text=text[candidate.pos : end],
            )
        )
    return sections if len(sections) >= MIN_SECTIONS else [_full_section(text)]
