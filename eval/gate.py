"""Threshold logic for the replay eval gate (pure, unit-testable).

The CLI in ``scripts/eval_gate.py`` only loads files and prints; decisions live
here so "would this PR be blocked?" can be tested without running an agent.
"""

from __future__ import annotations

from dataclasses import dataclass

# A block means "do not merge"; a warning is shown but does not fail the build.
CITATION_DROP_BLOCK = 0.02
FALSE_REFUSAL_RISE_BLOCK = 0.05
CORRECTNESS_DROP_WARN = 0.05
COST_RISE_WARN = 0.30


@dataclass(frozen=True, slots=True)
class GateDecision:
    blocks: list[str]
    warns: list[str]

    @property
    def blocked(self) -> bool:
        return bool(self.blocks)


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def evaluate(base: dict[str, float], current: dict[str, float]) -> GateDecision:
    blocks: list[str] = []
    warns: list[str] = []

    # A replay that silently drops items must never look like a pass: fewer
    # questions can hide a regression behind a smaller denominator.
    if int(current.get("n") or 0) != int(base.get("n") or 0):
        blocks.append(f"replay incomplete: n={current.get('n')} vs baseline {base.get('n')}")

    if current.get("citation_support", 0) <= base.get("citation_support", 0) - CITATION_DROP_BLOCK:
        blocks.append(
            f"引用支撑率 {pct(base.get('citation_support', 0))}"
            f" → {pct(current.get('citation_support', 0))}"
        )
    if (
        current.get("false_refusal_rate", 0)
        >= base.get("false_refusal_rate", 0) + FALSE_REFUSAL_RISE_BLOCK
    ):
        blocks.append(
            f"误拒率 {pct(base.get('false_refusal_rate', 0))}"
            f" → {pct(current.get('false_refusal_rate', 0))}"
        )
    if current.get("correctness", 0) <= base.get("correctness", 0) - CORRECTNESS_DROP_WARN:
        warns.append(
            f"正确率 {pct(base.get('correctness', 0))} → {pct(current.get('correctness', 0))}"
        )
    if current.get("cost_per_question", 0) >= base.get("cost_per_question", 0) * (
        1 + COST_RISE_WARN
    ):
        warns.append(
            f"单题成本 ${base.get('cost_per_question', 0):.5f}"
            f" → ${current.get('cost_per_question', 0):.5f}"
        )
    return GateDecision(blocks=blocks, warns=warns)
