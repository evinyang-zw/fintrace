"""Trace viewer: one run, step by step, with the cited text next to the claim.

Deliberately small. The value is not the UI, it is that every answer can be
replayed as a sequence of decisions and checked against the corpus without
reading JSON by hand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit as st

from pipelines.config import Settings
from pipelines.db import connect


def load_traces(trace_dir: Path) -> list[Path]:
    if not trace_dir.is_dir():
        return []
    return sorted(trace_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)


def load_trace(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def section_text(database_url: str, accession: str, item_code: str) -> tuple[int, str] | None:
    try:
        with connect(database_url) as conn:
            row = conn.execute(
                "SELECT char_start, text FROM sections WHERE accession = %s AND item_code = %s",
                (accession, item_code),
            ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return int(row["char_start"]), str(row["text"])


def locate_span(
    citation: dict[str, Any], section_start: int, text: str
) -> tuple[str, str, str] | None:
    """Return (before, hit, after) for a citation, or None if the span is invalid.

    Kept separate from Streamlit so the offset maths can be unit-tested without a
    browser: a wrong offset here would silently highlight the wrong paragraph.
    """
    start = int(citation["char_start"]) - section_start
    end = int(citation["char_end"]) - section_start
    if not (0 <= start < end <= len(text)):
        return None
    return text[max(0, start - 220) : start], text[start:end], text[end : end + 220]


def render_evidence(citation: dict[str, Any], database_url: str) -> None:
    found = section_text(database_url, str(citation["accession"]), str(citation["item_code"]))
    if found is None:
        st.warning(f"找不到原文：{citation['accession']}#{citation['item_code']}")
        return
    offset, text = found
    located = locate_span(citation, offset, text)
    if located is None:
        st.warning("引用区间越界，可能来自旧版语料")
        return
    before, hit, after = located
    st.markdown(
        '<div style="font-size:0.85rem;line-height:1.6;white-space:pre-wrap;'
        'border-left:3px solid #888;padding-left:10px">'
        f"{before}"
        f'<mark style="background:#ffd54f">{hit}</mark>'
        f"{after}</div>",
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title="FinTrace trace", layout="wide")
    settings = Settings.from_env()
    trace_dir = Path(settings.data_dir) / "traces"
    traces = load_traces(trace_dir)
    st.title("FinTrace · trace 回看")

    if not traces:
        st.info('还没有 trace。先跑 `make eval AGENT=react` 或 `make ask Q="…"`。')
        return

    labels = [path.name for path in traces]
    chosen = st.sidebar.selectbox("选择一次运行", labels)
    payload = load_trace(trace_dir / str(chosen))
    question = payload["question"]
    answer = payload.get("answer") or {}
    meta = payload.get("meta") or {}

    st.subheader(question["question"])
    st.caption(
        f"id={question['id']} · as_of={question['as_of']} · "
        f"model={meta.get('model', '?')} · prompt={meta.get('prompt_version', '?')}"
    )

    usage = answer.get("usage", {})
    cols = st.columns(5)
    cols[0].metric("步骤", len(payload.get("steps", [])))
    cols[1].metric("工具调用", usage.get("tool_calls", 0))
    cols[2].metric("成本", f"${usage.get('cost_usd', 0):.4f}")
    cols[3].metric("输入 token", usage.get("tokens_in", 0))
    cols[4].metric("延迟", f"{usage.get('latency_ms', 0)}ms")

    if answer.get("refused"):
        st.warning(f"拒答：{answer.get('refusal_reason')}")
    st.markdown("### 答案")
    st.write(answer.get("text", "（无）"))

    citations = answer.get("citations", [])
    if citations:
        st.markdown("### 引用与原文")
        for index, citation in enumerate(citations, start=1):
            with st.expander(
                f"[{index}] {citation['accession']}#{citation['item_code']} "
                f"({citation['char_start']}–{citation['char_end']})"
            ):
                render_evidence(citation, settings.database_url)

    st.markdown("### 决策轨迹")
    for step in payload.get("steps", []):
        ok = "✅" if step["ok"] else f"❌ {step.get('code')}"
        with st.expander(f"{step['index']}. {step['action']} {ok}", expanded=False):
            if step.get("thought"):
                st.markdown(f"**thought**：{step['thought']}")
            if step.get("args"):
                st.json(step["args"])
            st.text(str(step.get("observation", ""))[:4000])
            st.caption(
                f"tool {step.get('tool_latency_ms', 0)}ms · "
                f"tokens {step.get('tokens_in', 0)}/{step.get('tokens_out', 0)}"
            )

    if payload.get("notes"):
        st.markdown("### 备注")
        for note in payload["notes"]:
            st.text(note)


if __name__ == "__main__":
    main()
