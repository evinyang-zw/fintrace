from __future__ import annotations

import json
from datetime import date

from eval.metrics import ItemResult, aggregate
from eval.report import render_markdown, write_reports
from eval.schema import Answer, Citation, Grade, Question, Usage


def question(qid: str, *, must_refuse: bool = False, category: str = "fact") -> Question:
    return Question(
        id=qid,
        category=category,  # type: ignore[arg-type]
        question="营收是多少？",
        as_of=date(2025, 1, 1),
        universe=["MSFT"],
        must_refuse=must_refuse,
    )


def results() -> list[ItemResult]:
    return [
        ItemResult(
            question=question("a1"),
            answer=Answer(
                text="391,035 百万美元",
                citations=[
                    Citation(
                        accession="0001",
                        item_code="8",
                        char_start=0,
                        char_end=10,
                        quote="revenue",
                    )
                ],
                usage=Usage(latency_ms=1200, cost_usd=0.01),
            ),
            grade=Grade(correctness=2, citation_support=1.0),
        ),
        ItemResult(
            question=question("r1", must_refuse=True, category="unanswerable"),
            answer=Answer(text="无法确认", refused=True),
            grade=Grade(correctness=None, citation_support=1.0),
        ),
    ]


def test_render_contains_metrics_and_category_table() -> None:
    items = results()
    markdown = render_markdown(
        aggregate(items), items, agent="null", judge="rule-stub", dataset="x.jsonl"
    )
    assert "答案正确率" in markdown
    assert "引用支撑率" in markdown
    assert "F1 事实" in markdown or "fact" in markdown
    assert "rule-stub" in markdown


def test_render_lists_failures() -> None:
    items = [
        ItemResult(
            question=question("a1"),
            answer=Answer(text="胡编", refused=True),
            grade=Grade(correctness=0, citation_support=0.0, notes="false refusal"),
        )
    ]
    markdown = render_markdown(
        aggregate(items), items, agent="null", judge="rule-stub", dataset="x.jsonl"
    )
    assert "失败样本" in markdown
    assert "a1" in markdown
    assert "false refusal" in markdown


def test_write_reports_roundtrip(tmp_path) -> None:
    items = results()
    md_path, json_path = write_reports(
        aggregate(items),
        items,
        tmp_path,
        agent="null",
        judge="rule-stub",
        dataset="x.jsonl",
    )
    assert md_path.exists() and json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["agent"] == "null"
    assert payload["metrics"]["n"] == 2
    assert [item["id"] for item in payload["items"]] == ["a1", "r1"]


def test_report_structure_matches_the_documented_contract() -> None:
    items = results()
    markdown = render_markdown(
        aggregate(items), items, agent="null", judge="rule-stub", dataset="x.jsonl"
    )
    # header metadata (not a section)
    for key in ("数据集：", "判官：", "生成时间：", "说明："):
        assert key in markdown
    # exactly three body sections, in order
    headings = [line for line in markdown.splitlines() if line.startswith("## ")]
    assert headings == ["## 总览", "## 分类别", "## 失败样本"]
    # W1 has no baseline column yet
    assert "基线" not in markdown
