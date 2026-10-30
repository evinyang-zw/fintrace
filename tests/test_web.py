"""The trace viewer's offset maths — a wrong highlight is worse than none."""

from __future__ import annotations

from web.trace_app import locate_span

SECTION = "Item 7. MD&A\nTotal revenue was 391,035 million dollars, up 2%."
START = 10_000  # section offset inside the document


def citation(start: int, end: int) -> dict[str, int]:
    return {"char_start": start, "char_end": end}


def test_locate_span_inside_section() -> None:
    index = SECTION.index("391,035")
    located = locate_span(citation(START + index, START + index + 7), START, SECTION)
    assert located is not None
    before, hit, after = located
    assert hit == "391,035"
    assert before.endswith("revenue was ")
    assert after.startswith(" million")


def test_locate_span_rejects_out_of_bounds() -> None:
    assert locate_span(citation(START, START + 1), START, SECTION) is not None
    assert locate_span(citation(START - 1, START + 5), START, SECTION) is None
    assert (
        locate_span(citation(START + len(SECTION), START + len(SECTION) + 1), START, SECTION)
        is None
    )
    assert locate_span(citation(START + 5, START + 5), START, SECTION) is None


def test_locate_span_clamps_context_at_edges() -> None:
    located = locate_span(citation(START, START + 7), START, SECTION)
    assert located is not None
    before, _hit, _after = located
    assert before == ""  # no negative slice
