from __future__ import annotations

from datetime import date

from eval.agents.null import NullAgent
from eval.judge import RuleJudge
from eval.runner import load_dataset, run
from eval.schema import Answer, Question


def question(qid: str, category: str = "fact", must_refuse: bool = False) -> Question:
    return Question(
        id=qid,
        category=category,  # type: ignore[arg-type]
        question="q?",
        as_of=date(2025, 1, 1),
        universe=["MSFT"],
        must_refuse=must_refuse,
    )


async def test_null_agent_on_answerable_is_a_false_refusal() -> None:
    results = await run([question("a1")], NullAgent(), RuleJudge())
    assert len(results) == 1
    assert results[0].answer.refused is True
    assert results[0].grade.correctness == 0


async def test_null_agent_on_unanswerable_is_correct_refusal() -> None:
    results = await run(
        [question("r1", "unanswerable", must_refuse=True)], NullAgent(), RuleJudge()
    )
    assert results[0].grade.correctness is None
    assert results[0].grade.hallucination is False


async def test_results_keep_dataset_order() -> None:
    questions = [question("a1"), question("a2"), question("a3")]
    results = await run(questions, NullAgent(), RuleJudge(), concurrency=3)
    assert [r.question.id for r in results] == ["a1", "a2", "a3"]


def test_load_dataset_rejects_bad_line(tmp_path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "x"}\n', encoding="utf-8")
    try:
        load_dataset(path)
    except ValueError as exc:
        assert "invalid question" in str(exc)
        return
    raise AssertionError("expected ValueError")


def test_load_dataset_skips_blank_lines(tmp_path) -> None:
    path = tmp_path / "ok.jsonl"
    path.write_text(question("a1").model_dump_json() + "\n\n", encoding="utf-8")
    assert len(load_dataset(path)) == 1


def test_rule_judge_requires_well_formed_citations() -> None:
    judge = RuleJudge()
    q = question("a1")
    bad = Answer(text="x", citations=[])
    good = Answer(
        text="x",
        citations=[
            {
                "accession": "a",
                "item_code": "7",
                "char_start": 0,
                "char_end": 5,
                "quote": "hello",
            }
        ],
    )
    import asyncio

    assert asyncio.run(judge.grade(q, bad)).citation_support == 0.0
    assert asyncio.run(judge.grade(q, good)).citation_support == 1.0
