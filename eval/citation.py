"""Claim-level citation support (W3).

Two kinds of evidence, two kinds of proof:

* ``[K#]`` cites a text span — an LLM judges entailed / partial / unrelated;
* ``[F#]`` cites an XBRL fact — the number is compared to the fact value
  deterministically, because "the paragraph mentions 391,035" is not the same
  as "this number is that fact".

W2 only proved a citation *exists* and that its quote matches the corpus. That
let a revenue sentence cite an Item 8 index page and still score 100%. This
module asks the only question that matters: does the cited evidence support the
sentence?
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from agent.llm import LLMClient, LLMError, Message, build_llm
from eval.graders import numbers_match
from eval.schema import Answer, Citation, FactCitation, Question
from pipelines.config import Settings
from pipelines.db import connect

_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_HANDLE_RE = re.compile(r"\[([KF]\d+)\]")
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
NUMERIC_TOLERANCE = 0.005

SectionLookup = Callable[[str, str], tuple[int, str] | None]

_SYSTEM = """你是一个严格的引用校验器。逐条判断：句子是否被它引用的证据支持。

标签只有三种：
- supported：证据中有一句直接支持句子的核心主张
- partial：只支持一部分，或需要额外推断才能成立
- unsupported：证据与主张无关、只沾边，或与主张矛盾

注意：证据可能是一大段文字（甚至整节）。只要其中有一处直接支持该主张，就记
supported；不要因为段落里还有别的内容而扣分。反之，如果整段都没提到主张里的
关键数字或事实，记 unsupported。

只输出 JSON：{"results": [{"i": 1, "label": "supported", "why": "<=20字"}]}
i 必须与输入编号一致，覆盖全部条目，不要遗漏。
"""


@dataclass(frozen=True, slots=True)
class Claim:
    text: str
    handles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CitationReport:
    score: float
    supported: int
    partial: int
    unsupported: int
    uncited: int
    total: int
    notes: str

    def as_note(self) -> str:
        return (
            f"citation: {self.supported} supported / {self.partial} partial / "
            f"{self.unsupported} unsupported / {self.uncited} uncited (score {self.score:.2f})"
        )


def extract_claims(text: str) -> tuple[list[Claim], int]:
    """Split into cited claims and count sentences with no handle at all."""
    claims: list[Claim] = []
    uncited = 0
    for sentence in _SENTENCE_RE.findall(text):
        stripped = sentence.strip()
        if not stripped:
            continue
        handles = tuple(dict.fromkeys(_HANDLE_RE.findall(stripped)))
        if handles:
            claims.append(Claim(text=stripped, handles=handles))
        elif len(stripped) > 4:
            uncited += 1
    return claims, uncited


class CitationChecker:
    name = "citation-llm"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: LLMClient | None = None,
        section_lookup: SectionLookup | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm if llm is not None else (build_llm(settings) if settings else None)
        self._lookup = section_lookup or self._db_lookup

    def _db_lookup(self, accession: str, item_code: str) -> tuple[int, str] | None:
        if self._settings is None:
            return None
        with connect(self._settings.database_url) as conn:
            row = conn.execute(
                "SELECT text, char_start FROM sections WHERE accession = %s AND item_code = %s",
                (accession, item_code),
            ).fetchone()
        if row is None:
            return None
        return int(cast(int, row["char_start"])), str(row["text"])

    async def check(self, question: Question, answer: Answer) -> CitationReport:
        if answer.refused:
            return CitationReport(0.0, 0, 0, 0, 0, 0, "citation: refused")
        claims, uncited = extract_claims(answer.text)
        if not claims:
            return CitationReport(
                0.0, 0, 0, 0, uncited, 0, f"citation: no cited claim ({uncited} uncited)"
            )

        labels: list[str | None] = [None] * len(claims)
        pending: list[dict[str, Any]] = []
        fact_map = {fact.handle: fact for fact in answer.facts}
        text_map = {citation.handle: citation for citation in answer.citations if citation.handle}

        for index, claim in enumerate(claims):
            fact_handles = [handle for handle in claim.handles if handle.startswith("F")]
            if fact_handles:
                labels[index] = self._fact_label(claim.text, fact_handles, fact_map)
                continue
            spans = [
                self._span_text(text_map[handle])
                for handle in claim.handles
                if handle.startswith("K") and handle in text_map
            ]
            pending.append(
                {
                    "i": index + 1,
                    "claim": claim.text,
                    "spans": [span for span in spans if span][:3],
                }
            )

        if pending and self._llm is not None:
            try:
                result = await self._llm.complete(
                    [
                        Message("system", _SYSTEM),
                        Message("user", json.dumps(pending, ensure_ascii=False, indent=2)),
                    ],
                    max_tokens=800,
                    json_mode=True,
                )
                parsed = self._parse(result.text)
            except LLMError as exc:
                return CitationReport(0.0, 0, 0, 0, uncited, len(claims), f"citation_error: {exc}")
            for entry in pending:
                labels[int(entry["i"]) - 1] = parsed.get(int(entry["i"]), "unsupported")
        else:
            for entry in pending:
                labels[int(entry["i"]) - 1] = "unsupported"

        final = [label or "unsupported" for label in labels]
        supported = sum(1 for label in final if label == "supported")
        partial = sum(1 for label in final if label == "partial")
        unsupported = sum(1 for label in final if label == "unsupported")
        score = (supported + 0.5 * partial) / len(claims)
        return CitationReport(
            score=score,
            supported=supported,
            partial=partial,
            unsupported=unsupported,
            uncited=uncited,
            total=len(claims),
            notes="",
        )

    @staticmethod
    def _fact_label(claim: str, handles: list[str], facts: dict[str, FactCitation]) -> str:
        for handle in handles:
            fact = facts.get(handle)
            if fact is None:
                continue
            matched, _best = numbers_match(claim, fact.value, NUMERIC_TOLERANCE)
            if matched:
                return "supported"
        return "unsupported"

    def _span_text(self, citation: Citation) -> str:
        section = self._lookup(citation.accession, citation.item_code)
        if section is not None:
            section_start, text = section
            start = max(citation.char_start - section_start, 0)
            end = min(citation.char_end - section_start, len(text))
            segment = text[start:end].strip()
            if segment:
                # Long spans get head+tail: a number stated in the middle of a
                # 4k character section must not be invisible to the checker.
                if len(segment) > 6000:
                    return segment[:3000] + "\n…\n" + segment[-3000:]
                return segment
        return citation.quote.strip()[:6000]

    @staticmethod
    def _parse(text: str) -> dict[int, str]:
        cleaned = _FENCE.sub("", text.strip())
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return {}
        raw = cast(dict[str, Any], payload).get("results")
        if not isinstance(raw, list):
            return {}
        out: dict[int, str] = {}
        for item in raw:
            entry = cast(dict[str, Any], item)
            try:
                index = int(entry.get("i", 0))
            except (TypeError, ValueError):
                continue
            label = str(entry.get("label", "")).lower()
            if label in {"supported", "partial", "unsupported"}:
                out[index] = label
        return out
