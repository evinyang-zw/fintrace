"""HTML -> plain text.

Offsets stored in the database are offsets into *this* normalised text, not into
the original HTML. That keeps citation spans stable regardless of markup noise,
and it is why ``section.char_start`` always refers to ``section.text``.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

_DROP_TAGS = ("script", "style", "noscript", "ix:header")
_BLOCK_TAGS = (
    "p",
    "div",
    "br",
    "tr",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "section",
    "article",
)
_MULTI_SPACE = re.compile(r"[ \t\u00a0]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    for tag in soup(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = soup.get_text()
    lines = [_MULTI_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    normalised = "\n".join(line for line in lines if line)
    return _MULTI_NEWLINE.sub("\n\n", normalised).strip()


def normalize_whitespace(text: str) -> str:
    return _MULTI_SPACE.sub(" ", text.replace("\n", " ")).strip()
