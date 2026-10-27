from __future__ import annotations

import itertools

from pipelines.chunking import chunk_text


def test_short_text_single_chunk() -> None:
    chunks = chunk_text("hello world")
    assert len(chunks) == 1
    assert chunks[0].text == "hello world"


def test_empty_text_no_chunks() -> None:
    assert chunk_text("   \n\n ") == []


def test_long_text_offsets_and_coverage() -> None:
    paragraph = "A" * 500 + "\n\n"
    text = paragraph * 40
    chunks = chunk_text(text, target_chars=3200, overlap_chars=400)
    assert len(chunks) > 1
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(text)
    for chunk in chunks:
        assert chunk.char_end > chunk.char_start
        assert chunk.text == text[chunk.char_start : chunk.char_end]
    for current, following in itertools.pairwise(chunks):
        assert following.char_start <= current.char_end


def test_never_returns_empty_chunk_for_whitespace() -> None:
    text = ("word " * 2000) + "\n" + ("  \n" * 500)
    for chunk in chunk_text(text):
        assert chunk.text.strip()
