"""The gate decision is pure, so the blocking rules are testable without an agent."""

from __future__ import annotations

from eval.gate import evaluate


def _metrics(**overrides: float | int) -> dict:
    base: dict = {
        "n": 10,
        "correctness": 0.9,
        "citation_support": 0.9,
        "false_refusal_rate": 0.1,
        "cost_per_question": 0.001,
    }
    base.update(overrides)
    return base


def test_identical_metrics_pass() -> None:
    decision = evaluate(_metrics(), _metrics())
    assert decision.blocked is False
    assert decision.warns == []


def test_fewer_replay_items_block() -> None:
    decision = evaluate(_metrics(), _metrics(n=9))
    assert decision.blocked is True
    assert "replay incomplete" in decision.blocks[0]


def test_citation_drop_blocks() -> None:
    decision = evaluate(_metrics(), _metrics(citation_support=0.87))
    assert decision.blocked is True
    assert "引用支撑率" in decision.blocks[0]


def test_small_citation_drop_is_tolerated() -> None:
    decision = evaluate(_metrics(), _metrics(citation_support=0.89))
    assert decision.blocked is False


def test_false_refusal_rise_blocks() -> None:
    decision = evaluate(_metrics(), _metrics(false_refusal_rate=0.16))
    assert decision.blocked is True
    assert "误拒率" in decision.blocks[0]


def test_correctness_drop_only_warns() -> None:
    decision = evaluate(_metrics(), _metrics(correctness=0.84))
    assert decision.blocked is False
    assert "正确率" in decision.warns[0]


def test_cost_rise_only_warns() -> None:
    decision = evaluate(_metrics(), _metrics(cost_per_question=0.0014))
    assert decision.blocked is False
    assert "单题成本" in decision.warns[0]


def test_missing_metric_keys_do_not_crash() -> None:
    decision = evaluate({}, {})
    assert decision.blocked is False
