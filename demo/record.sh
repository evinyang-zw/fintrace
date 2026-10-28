#!/usr/bin/env bash
# Recorded by asciinema in demo/README. Run: bash demo/record.sh
set -euo pipefail
cd "$(dirname "$0")/.."

bold() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
dim() { printf '\033[2m%s\033[0m\n' "$*"; }

printf "\033[2J\033[H"
printf '\033[1mFinTrace — W1: 地基与标尺\033[0m\n'
dim '今天不讲问答。只证明两件事：数据能进来，系统能被打分。'
sleep 3

bold '① 仓库里现在有什么'
sleep 1
export PATH="$HOME/.local/bin:$PATH"
make -s stats
sleep 4

bold '② 摄取是幂等的：第二次跑全部跳过（快照缓存，离线可重放）'
sleep 1
make -s ingest
sleep 4

bold '③ 30 道金标题：每道数值题的金标都回 XBRL 重算对账'
sleep 1
make -s q-check
sleep 4

bold '④ 基线：NullAgent 永远拒答，所以正确率必须刚好是 0'
sleep 1
make -s eval
sleep 5

printf '\n\033[1;32m尺子已经准了，数据已经在了，Agent 还没写。\033[0m\n'
dim '这是故意的取舍，理由见 docs/adr/ADR-001-eval-first.md。'
sleep 4
