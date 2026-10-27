"""Naive paragraph-aware chunking.

Deliberately simple for W1: the ablation table (W3) needs a *naive* baseline to
compare against. Do not "improve" this before the baseline is measured.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TARGET_CHARS = 3200  # ~800 tokens
DEFAULT_OVERLAP_CHARS = 400


@dataclass(frozen=True, slots=True)
class Chunk:
    char_start: int
    char_end: int
    text: str

    @property
    def n_tokens(self) -> int:
        return max(1, len(self.text) // 4)


def chunk_text(
    text: str,
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    if len(text) <= target_chars:
        return [Chunk(char_start=0, char_end=len(text), text=text)] if text.strip() else []

    empty = not text.strip()
    if empty:
        return []

    chunks: list[Chunk] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        end = min(cursor + target_chars, length)
        if end < length:
            boundary = text.rfind("\n\n", cursor + target_chars // 2, end)
            if boundary == -1:
                boundary = text.rfind("\n", cursor + target_chars // 2, end)
            if boundary > cursor:
                end = boundary
        body = text[cursor:end]
        if body.strip():
            chunks.append(Chunk(char_start=cursor, char_end=end, text=body))
        if end >= length:
            break
        cursor = max(end - overlap_chars, cursor + 1)
    return chunks
