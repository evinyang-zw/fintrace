from __future__ import annotations

from datetime import date

from eval.check import _check_structure
from eval.schema import Question


def question(**overrides: object) -> Question:
    payload: dict[str, object] = {
        "id": "a1",
        "category": "fact",
        "question": "q?",
        "as_of": date(2025, 1, 1),
        "universe": ["MSFT"],
        "rubric": {"2": "correct", "0": "wrong"},
        "author_note": "answerable by Item 7",
    }
    payload.update(overrides)
    return Question.model_validate(payload)


def test_valid_question_has_no_errors() -> None:
    errors = [e for e in _check_structure([question()]) if "questions" not in e]
    assert errors == []


def test_duplicate_ids_are_rejected() -> None:
    errors = _check_structure([question(), question()])
    assert any("duplicate ids" in e for e in errors)


def test_unanswerable_must_not_have_gold() -> None:
    errors = _check_structure([question(must_refuse=True, gold={"value": 1.0, "unit": "USD"})])
    assert any("must not define gold" in e for e in errors)


def test_numeric_question_needs_gold() -> None:
    errors = _check_structure([question(category="calculation")])
    assert any("needs gold" in e for e in errors)


def test_missing_rubric_is_rejected() -> None:
    errors = _check_structure([question(rubric={})])
    assert any("rubric is empty" in e for e in errors)


def test_missing_author_note_is_rejected() -> None:
    errors = _check_structure([question(author_note="")])
    assert any("author_note missing" in e for e in errors)


def test_adversarial_question_needs_existing_corpus() -> None:
    errors = _check_structure(
        [question(category="adversarial", corpus="adversarial", corpus_ref="missing.html")]
    )
    assert any("corpus file not found" in e for e in errors)


def test_adversarial_question_with_real_corpus_passes_corpus_check() -> None:
    errors = _check_structure(
        [
            question(
                category="adversarial",
                corpus="adversarial",
                corpus_ref="northwind-10k-fy2024.html",
            )
        ]
    )
    assert not any("corpus" in e for e in errors)


def test_adversarial_question_without_ref_is_rejected() -> None:
    errors = _check_structure([question(category="adversarial", corpus="adversarial")])
    assert any("needs corpus_ref" in e for e in errors)
