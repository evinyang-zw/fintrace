"""The four tools the agent may call, and the evidence ledger behind them.

The ledger is the point. A tool does not return "some text"; it returns a
``K<n>`` handle bound to ``(accession, item, char_start, char_end)``. The model
may only cite handles, so a citation cannot point at a fragment that was never
retrieved. Semantic support is still a W3 problem; structural honesty is this
file's job.
"""

from __future__ import annotations

import ast
import json
import operator
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, cast

from agent.retrieval import Retriever
from eval.schema import Citation
from pipelines.db import connect

_MAX_SECTION_CHARS = 6000
_MAX_SNIPPET_CHARS = 400
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")
_SNIPPET_STOP = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "were",
    "was",
    "are",
    "its",
    "our",
    "has",
    "have",
    "had",
    "due",
    "primarily",
    "increase",
    "increased",
    "decrease",
    "decreased",
    "during",
    "compared",
    "which",
    "what",
    "how",
    "why",
    "fiscal",
    "year",
    "million",
    "billion",
}


def focus_snippet(text: str, query: str, limit: int = _MAX_SNIPPET_CHARS) -> str:
    """Return a window of ``text`` around the query terms, not the first N chars.

    Taking the head of a 3,200-character chunk is how the agent "reads" past the
    sentence it needs and then refuses. This picks the densest query-term window
    instead; ``get_section`` remains the way to read the whole section.
    """
    terms = {
        word.lower()
        for word in _WORD_RE.findall(query)
        if len(word) > 2 and word.lower() not in _SNIPPET_STOP
    }
    if not terms or len(text) <= limit:
        return text[:limit]
    low = text.lower()
    best_pos, best_hits = -1, 0
    # Sorted terms + "earliest position wins ties" makes the window independent
    # of set iteration order (PYTHONHASHSEED). Without this, the same query
    # produces a different snippet in a new process and replay/caching break.
    for term in sorted(terms):
        start = 0
        while (position := low.find(term, start)) != -1:
            window = low[max(0, position - limit // 2) : position + limit // 2]
            hits = sum(1 for candidate in terms if candidate in window)
            if hits > best_hits or (hits == best_hits and (best_pos < 0 or position < best_pos)):
                best_pos, best_hits = position, hits
            start = position + 1
            if hits == len(terms):
                break
    if best_pos < 0:
        return text[:limit]
    start = max(0, best_pos - limit // 3)
    end = min(len(text), start + limit)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


_MAX_QUOTE_CHARS = 200


_TAG_ALIASES: dict[str, tuple[str, ...]] = {
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "CapitalExpenditures",
    ),
    "capital expenditure": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "CapitalExpenditures",
    ),
    "capital expenditures": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "CapitalExpenditures",
    ),
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "net income": ("NetIncomeLoss", "ProfitLoss"),
    "operating income": ("OperatingIncomeLoss",),
    "gross profit": ("GrossProfit",),
    "research": ("ResearchAndDevelopmentExpense",),
}


class ToolArgError(ValueError):
    """Bad arguments; the model sees this and is expected to correct itself."""


class ToolFailure(RuntimeError):
    """A well-formed call that cannot succeed (missing data, ambiguous input)."""


@dataclass(frozen=True, slots=True)
class FactEvidence:
    name: str
    ticker: str
    tag: str
    value: float
    unit: str
    period_end: date
    fiscal_period: str | None = None


@dataclass(frozen=True, slots=True)
class Handle:
    name: str
    accession: str
    item_code: str
    char_start: int
    char_end: int
    quote: str
    label: str
    filed_at: date


@dataclass
class EvidenceStore:
    """Per-run map from handle to a concrete text span."""

    handles: dict[str, Handle] = field(default_factory=dict)
    # One entry per search call: which tier actually ran, and which section
    # identifiers came back. Recall@8 is computed from this without re-running
    # retrieval, so the metric and the answer always describe the same run.
    search_calls: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, FactEvidence] = field(default_factory=dict)
    # Retrieval usage for cost accounting; kept off the model-facing payload so
    # token counters never change a prompt (and therefore never break replay).
    embedding_tokens: int = 0
    rerank_tokens: int = 0
    _counter: int = 0
    _fact_counter: int = 0

    def add_fact(
        self,
        *,
        ticker: str,
        tag: str,
        value: float,
        unit: str,
        period_end: date,
        fiscal_period: str | None,
    ) -> FactEvidence:
        self._fact_counter += 1
        fact = FactEvidence(
            name=f"F{self._fact_counter}",
            ticker=ticker,
            tag=tag,
            value=value,
            unit=unit,
            period_end=period_end,
            fiscal_period=fiscal_period,
        )
        self.facts[fact.name] = fact
        return fact

    def add(
        self,
        *,
        accession: str,
        item_code: str,
        char_start: int,
        char_end: int,
        quote: str,
        label: str,
        filed_at: date,
    ) -> Handle:
        self._counter += 1
        handle = Handle(
            name=f"K{self._counter}",
            accession=accession,
            item_code=item_code,
            char_start=char_start,
            char_end=char_end,
            quote=quote[:_MAX_QUOTE_CHARS],
            label=label,
            filed_at=filed_at,
        )
        self.handles[handle.name] = handle
        return handle

    def get(self, name: str) -> Handle | None:
        return self.handles.get(name.strip().lstrip("[").rstrip("]"))

    def known(self) -> str:
        return ", ".join(self.handles) if self.handles else "(none yet)"

    def describe(self) -> str:
        if not self.handles:
            return "(none)"
        return "\n".join(
            f"- [{h.name}] {h.label} item {h.item_code} chars {h.char_start}-{h.char_end}"
            for h in self.handles.values()
        )


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    ok: bool
    data: dict[str, Any] | None = None
    code: str | None = None
    message: str | None = None

    def to_observation(self) -> str:
        if self.ok:
            return json.dumps(self.data, ensure_ascii=False, default=str)
        return f"ERROR[{self.code}]: {self.message}"


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_filings",
        "description": (
            "在指定公司的财报正文里检索，返回带句柄 [Kn] 的片段。"
            "query 用自然语言即可（中文或英文都行；英文通常更准）。"
            "只有这里和 get_section 返回的片段才能被引用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "自然语言或关键词，例如 'Apple total net revenue fiscal 2024'",
                },
                "tickers": {"type": "array", "items": {"type": "string"}},
                "as_of": {"type": "string", "description": "YYYY-MM-DD，只用该日期前披露的文档"},
                "k": {"type": "integer", "default": 8},
                "form": {"type": "string", "description": "可选：10-K / 10-Q"},
            },
            "required": ["query", "tickers", "as_of"],
        },
    },
    {
        "name": "get_section",
        "description": "取某份文档的某一节全文（截断 6000 字符）。用于核对检索片段之外的细节。",
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "文档 ID（search_filings 返回的 doc_id）",
                },
                "item": {"type": "string", "description": "章节号，例如 '7' / '8' / '1A'"},
            },
            "required": ["doc_id", "item"],
        },
    },
    {
        "name": "xbrl_fact",
        "description": (
            "查询 XBRL 结构化财务数字——所有数字的唯一权威来源。"
            "tag_query 用自然语言（如 'revenue' / 'net income' / 'capital expenditure'），"
            "会返回最多 5 个候选（含期间、单位与 F# 句柄），自己选对的那个。"
            "答案里的数字必须引用对应的 [F#]。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "tag_query": {"type": "string"},
                "as_of": {"type": "string", "description": "YYYY-MM-DD，只用该日期前申报的数字"},
                "period_end": {"type": "string", "description": "可选：YYYY-MM-DD，限定财年结束日"},
            },
            "required": ["ticker", "tag_query", "as_of"],
        },
    },
    {
        "name": "compute",
        "description": "四则运算，返回数值。不要心算大数或百分比，用这个工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "如 '391035/1000000' 或 '(120-100)/100'",
                }
            },
            "required": ["expression"],
        },
    },
]


def _req_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolArgError(f"{key} must be a non-empty string")
    return value.strip()


def _opt_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolArgError(f"{key} must be a string when present")
    return value.strip() or None


def _req_list(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if not isinstance(value, list) or not value:
        raise ToolArgError(f"{key} must be a non-empty array of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ToolArgError(f"{key} must contain non-empty strings")
        out.append(item.strip().upper())
    return out


def _req_date(args: dict[str, Any], key: str) -> date:
    raw = args.get(key)
    if not isinstance(raw, str):
        raise ToolArgError(f"{key} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ToolArgError(f"{key} must be YYYY-MM-DD, got {raw!r}") from exc


def _opt_date(args: dict[str, Any], key: str) -> date | None:
    raw = args.get(key)
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise ToolArgError(f"{key} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ToolArgError(f"{key} must be YYYY-MM-DD, got {raw!r}") from exc


def _opt_int(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    raw = args.get(key, default)
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ToolArgError(f"{key} must be an integer")
    return max(low, min(high, raw))


_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS: dict[type[ast.unaryop], Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def safe_compute(expression: str) -> float:
    """Evaluate arithmetic only: no names, no calls, no exponents."""
    if len(expression) > 200:
        raise ToolArgError("expression too long (max 200 chars)")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolArgError(f"not a valid arithmetic expression: {expression!r}") from exc

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise ToolArgError("division by zero")
            return float(_BIN_OPS[type(node.op)](left, right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return float(_UNARY_OPS[type(node.op)](walk(node.operand)))
        raise ToolArgError(f"unsupported expression element: {type(node).__name__}")

    value = walk(tree)
    if value != value or value in (float("inf"), float("-inf")):
        raise ToolArgError("result is not a finite number")
    return value


class ToolExecutor:
    def __init__(
        self,
        database_url: str,
        store: EvidenceStore,
        *,
        retrieval: Retriever | None = None,
    ) -> None:
        self._database_url = database_url
        self._store = store
        self._retrieval = retrieval or Retriever(database_url)

    def execute(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        handler = {
            "search_filings": self._search,
            "get_section": self._get_section,
            "xbrl_fact": self._xbrl,
            "compute": self._compute,
        }.get(name)
        if handler is None:
            return ToolOutcome(ok=False, code="unknown_tool", message=f"no such tool: {name}")
        try:
            return ToolOutcome(ok=True, data=handler(args))
        except ToolArgError as exc:
            return ToolOutcome(ok=False, code="bad_args", message=str(exc))
        except ToolFailure as exc:
            return ToolOutcome(ok=False, code="not_found", message=str(exc))
        except Exception as exc:  # a tool must never kill the loop
            return ToolOutcome(ok=False, code="internal", message=f"{type(exc).__name__}: {exc}")

    # -- individual tools -------------------------------------------------

    def _resolve(self, tickers: Sequence[str]) -> dict[str, str]:
        with connect(self._database_url) as conn:
            rows = conn.execute(
                "SELECT ticker, cik FROM companies WHERE ticker = ANY(%s)", (list(tickers),)
            ).fetchall()
        mapping = {str(row["ticker"]): str(row["cik"]) for row in rows}
        missing = [t for t in tickers if t not in mapping]
        if missing:
            raise ToolFailure(f"unknown ticker(s): {', '.join(missing)}")
        return mapping

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = _req_str(args, "query")
        tickers = _req_list(args, "tickers")
        as_of = _req_date(args, "as_of")
        k = _opt_int(args, "k", 8, 1, 20)
        form = _opt_str(args, "form")
        mapping = self._resolve(tickers)

        hits, meta = self._retrieval.search(query, list(mapping.values()), as_of, k=k, form=form)
        self._store.embedding_tokens += meta.embedding_tokens
        self._store.rerank_tokens += meta.rerank_tokens
        if not hits:
            payload: dict[str, Any] = {
                "results": [],
                "retrieval": meta.as_dict(),
                "hint": (
                    "no match. Try broader keywords, drop the year, "
                    "or use get_section on a known item."
                ),
            }
            if meta.degraded:
                payload["warning"] = "retrieval degraded: " + ", ".join(meta.degraded)
            return payload

        results: list[dict[str, Any]] = []
        for hit in hits:
            handle = self._store.add(
                accession=hit.accession,
                item_code=hit.item_code,
                char_start=hit.char_start,
                char_end=hit.char_end,
                quote=hit.text,
                label=f"{hit.ticker}/{hit.form}/{hit.period_end}",
                filed_at=hit.filed_at,
            )
            results.append(
                {
                    "handle": handle.name,
                    "doc_id": hit.accession,
                    "ticker": hit.ticker,
                    "form": hit.form,
                    "item": hit.item_code,
                    "period_end": str(hit.period_end),
                    "filed_at": str(hit.filed_at),
                    "score": round(hit.score, 4),
                    "snippet": focus_snippet(hit.text, query),
                    "section_chars": len(hit.text),
                    "snippet_truncated": len(hit.text) > _MAX_SNIPPET_CHARS,
                }
            )

        self._store.search_calls.append(
            {
                "query": query,
                "tier": meta.tier,
                "degraded": list(meta.degraded),
                "hits": [hit.citation_key for hit in hits],
            }
        )
        payload = {
            "results": results,
            "retrieval": meta.as_dict(),
            "note": (
                "snippet 只是片段。若其中没有你要的具体数字、原因或表述，"
                "调用 get_section(doc_id, item) 读该节全文，再决定是否拒答。"
            ),
        }
        if meta.degraded:
            payload["warning"] = (
                "retrieval degraded to "
                + meta.tier
                + ": "
                + ", ".join(meta.degraded)
                + ". Say so if it limits the answer."
            )
        return payload

    def _get_section(self, args: dict[str, Any]) -> dict[str, Any]:
        doc_id = _req_str(args, "doc_id")
        item = re.sub(r"^item\s*", "", _req_str(args, "item"), flags=re.IGNORECASE).strip()
        sql = """
            SELECT s.text, s.char_start, s.char_end, s.item_code, f.form, f.filed_at,
                   f.period_end, c.ticker
            FROM sections s
            JOIN filings f ON f.accession = s.accession
            JOIN companies c ON c.cik = f.cik
            WHERE s.accession = %(doc)s AND s.item_code = %(item)s
        """
        with connect(self._database_url) as conn:
            row = conn.execute(sql, {"doc": doc_id, "item": item}).fetchone()
            if row is None:
                row = self._section_inside_full(conn, doc_id, item)
        if row is None:
            raise ToolFailure(f"section {doc_id}#{item} not found; check doc_id and item")

        text = str(row["text"])
        clipped = text[:_MAX_SECTION_CHARS]
        offset = int(cast(int, row["char_start"]))
        handle = self._store.add(
            accession=doc_id,
            item_code=str(row["item_code"]),
            char_start=offset,
            char_end=offset + len(clipped),
            quote=clipped,
            label=f"{row['ticker']}/{row['form']}/{row['period_end']}",
            filed_at=cast(date, row["filed_at"]),
        )
        return {
            "handle": handle.name,
            "doc_id": doc_id,
            "item": item,
            "chars": [offset, offset + len(clipped)],
            "truncated": len(clipped) < len(text),
            "text": clipped,
        }

    def _section_inside_full(self, conn: Any, accession: str, item: str) -> dict[str, Any] | None:
        """Synthetic filings often fall back to one FULL section; slice it again."""
        row = conn.execute(
            """
            SELECT s.text, s.char_start, f.form, f.filed_at, f.period_end, c.ticker
            FROM sections s
            JOIN filings f ON f.accession = s.accession
            JOIN companies c ON c.cik = f.cik
            WHERE s.accession = %(doc)s AND s.item_code = 'FULL'
            """,
            {"doc": accession},
        ).fetchone()
        if row is None:
            return None
        text = str(row["text"])
        heading = re.search(rf"(?im)^\s*item\s*{re.escape(item)}[.\s\u2014-]", text)
        if heading is None:
            return None
        start = heading.start()
        following = re.search(r"(?im)^\s*item\s+\d+[a-z]?[.\s\u2014-]", text[heading.end() :])
        end = heading.end() + following.start() if following else len(text)
        offset = int(cast(int, row["char_start"])) + start
        return {
            "text": text[start:end],
            "char_start": offset,
            "char_end": offset + (end - start),
            "item_code": "FULL",
            "form": row["form"],
            "filed_at": row["filed_at"],
            "period_end": row["period_end"],
            "ticker": row["ticker"],
        }

    def _xbrl(self, args: dict[str, Any]) -> dict[str, Any]:
        ticker = _req_str(args, "ticker").upper()
        tag_query = _req_str(args, "tag_query").lower()
        as_of = _req_date(args, "as_of")
        period_end = _opt_date(args, "period_end")
        mapping = self._resolve([ticker])
        cik = mapping[ticker]

        tokens = [t for t in re.split(r"[^a-z0-9]+", tag_query) if t]
        base_pattern = "%" + "%".join(tokens) + "%" if tokens else "%"
        alias_hits: list[tuple[int, str]] = []
        for alias, tags in _TAG_ALIASES.items():
            if alias in tag_query:
                alias_hits.extend((index, tag) for index, tag in enumerate(tags))
        patterns = [base_pattern, *(f"%{tag.lower()}%" for _, tag in alias_hits)]

        sql = """
            WITH matched AS (
                SELECT tag, unit, period_end, fiscal_year, fiscal_period, value,
                       filed_at, form, accn,
                       row_number() OVER (
                           PARTITION BY tag, period_end ORDER BY filed_at DESC
                       ) AS rn
                FROM facts
                WHERE cik = %(cik)s
                  AND filed_at <= %(as_of)s
                  AND lower(tag) LIKE ANY(%(patterns)s)
                  AND (%(period_end)s::date IS NULL OR period_end = %(period_end)s)
            )
            SELECT * FROM matched
            WHERE rn = 1
            ORDER BY period_end DESC
            LIMIT 60
        """

        def query(period: date | None) -> list[dict[str, Any]]:
            with connect(self._database_url) as conn:
                return conn.execute(
                    sql,
                    {
                        "cik": cik,
                        "as_of": as_of,
                        "patterns": patterns,
                        "period_end": period,
                    },
                ).fetchall()

        period_miss = False
        rows = query(period_end)
        if not rows and period_end is not None:
            # A guessed period_end (e.g. Dec 31 for a company whose fiscal year
            # ends Dec 28) must not turn into "no data": retry without it and say so.
            rows, period_miss = query(None), True

        if not rows:
            return {
                "matches": [],
                "hint": (
                    "no fact matched. Try a shorter accounting term "
                    "('revenue', 'net income', 'operating income', 'capex')."
                ),
            }

        # Rank in Python: exact tag > known alias (by curated priority) >
        # prefix > substring; newest period first inside a rank.
        priority: dict[str, int] = {}
        for index, tag in alias_hits:
            priority.setdefault(tag.lower(), index)

        def rank(row: dict[str, Any]) -> tuple[int, int]:
            tag = str(row["tag"]).lower()
            if tag == tag_query:
                return (0, 0)
            if tag in priority:
                return (1, priority[tag])
            if tag.startswith(tag_query):
                return (2, 0)
            return (3, 0)

        ranked = sorted(
            rows, key=lambda row: (rank(row), -cast(date, row["period_end"]).toordinal())
        )
        # A company may report the same tag for many quarters. Always surface the
        # latest annual row first, then the newest quarters, so an FY question can
        # find its fact without the model having to know the fiscal calendar.
        ordered: list[dict[str, Any]] = []
        for tag in dict.fromkeys(str(row["tag"]) for row in ranked):
            tag_rows = [row for row in ranked if str(row["tag"]) == tag]
            annual = [row for row in tag_rows if row["fiscal_period"] == "FY"]
            if annual:
                ordered.append(annual[0])
            ordered.extend(row for row in tag_rows if row not in annual[:1])
            if len(ordered) >= 5:
                break
        ordered = ordered[:5]

        matches: list[dict[str, Any]] = []
        for row in ordered:
            fact = self._store.add_fact(
                ticker=ticker,
                tag=str(row["tag"]),
                value=float(cast(Any, row["value"])),
                unit=str(row["unit"]),
                period_end=cast(date, row["period_end"]),
                fiscal_period=cast(str | None, row["fiscal_period"]),
            )
            matches.append(
                {
                    "handle": fact.name,
                    "tag": fact.tag,
                    "value": fact.value,
                    "unit": fact.unit,
                    "period_end": str(fact.period_end),
                    "fiscal_year": row["fiscal_year"],
                    "fiscal_period": row["fiscal_period"],
                    "form": row["form"],
                    "filed_at": str(row["filed_at"]),
                }
            )
        payload: dict[str, Any] = {
            "matches": matches,
            "hint": "引用数字时用 [F#]；不要用正文段落代替数字的出处。",
        }
        if period_miss:
            payload["period_end_miss"] = True
            payload["period_end_note"] = (
                "指定的 period_end 没有命中，已忽略它返回可用期间；请核对财年结束日。"
            )
        return payload

    def _compute(self, args: dict[str, Any]) -> dict[str, Any]:
        expression = _req_str(args, "expression")
        value = safe_compute(expression)
        return {"expression": expression, "value": round(value, 10)}


def citation_from(handle: Handle) -> Citation:
    return Citation(
        accession=handle.accession,
        item_code=handle.item_code,
        char_start=handle.char_start,
        char_end=handle.char_end,
        quote=handle.quote,
        handle=handle.name,
    )


def evidence_citations(store: EvidenceStore) -> list[Citation]:
    """All spans the run actually saw, de-duplicated, in handle order."""
    seen: set[tuple[str, str, int, int]] = set()
    out: list[Citation] = []
    for handle in store.handles.values():
        key = (handle.accession, handle.item_code, handle.char_start, handle.char_end)
        if key in seen:
            continue
        seen.add(key)
        out.append(citation_from(handle))
    return out
