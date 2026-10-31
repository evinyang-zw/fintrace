#!/usr/bin/env bash
# Three-tier ablation. One tier per process, because retrieval is configured by
# env; two seeds per tier so the report can show run-to-run variance.
set -euo pipefail
SEEDS="${SEEDS:-0 1}"
TIERS="${TIERS:-bm25 hybrid hybrid_rerank}"
for seed in $SEEDS; do
  for tier in $TIERS; do
    echo "== tier=${tier} seed=${seed} =="
    RETRIEVAL_TIER="${tier}" uv run python -m eval.runner \
      --agent react --judge llm --seed "${seed}" \
      --out "reports/ablation/${tier}-s${seed}"
  done
done
