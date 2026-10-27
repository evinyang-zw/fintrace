# W1 报告：地基与标尺

日期：2026-10-27 · 对应计划 `m0-w1-plan.md`

## 做完了什么

| 交付物 | 状态 | 证据 |
|---|---|---|
| A 摄取管道 | ✅ | `make ingest`：10 家公司 / 233 份 filing / 2,865 sections / 85,079 facts |
| B 金标题 | 🟡 6/30 | `eval/datasets/gold_v0.jsonl`，六类各 1 题种子 |
| C 指标口径 | ✅ | `docs/metrics.md`（含边界情况） + `eval/metrics.py` |
| D 评测运行器 | ✅ | `make eval` 出报告；NullAgent 可解释全零 |
| E CI 骨架 | ✅ | `.github/workflows/ci.yml`：ruff + mypy strict + pytest + eval smoke |

## 关键数字

- section 覆盖率：**100%**（21 份 2024 年后 filing 中仅 KO 10-Q/A 单节，属修正案正常形态）
- 单份 filing 平均 9–11 节（10-Q）/ 21–22 节（10-K）
- 入库耗时：约 6 分钟 / 233 份（含首次下载与 HTML 解析，之后走本地快照可离线重跑）

## 踩到的三个坑（都是真实 10-K 的形态，不是 bug 修完就忘的那种）

**1. 每页页眉都在重复 "Item 1" 和 "Table of Contents"。**

第一版用"最后一次出现 Table of Contents"定正文起点，META 的正文被砍到只剩 4 节。
改为：标题必须带标点（`Item 1.` / `Item 1—`），且用"序列选择"而不是位置启发式。

**2. 表格目录与正文的 item 序列重复。**

第一版按"覆盖字符跨度最大"选序列，TOC（前 1.2KB）+ 文末 Item 15 的组合赢了正文。
改为：**按链长（item 数）优先、起点更晚者优先**——TOC 链短于正文链时自然出局。

**3. 各家的标题格式不同。**

MSFT 用 `ITEM 1. BUSINESS`，META 用 `Item 1.Financial Statements`（无空格），
COST 用 `Item 1—Financial Statements`（破折号），META 的分部标题是
`PART II—OTHER INFORMATION`（带后缀）。正则统一覆盖，仍不匹配的留给 per-company override。

## 下一步（W1 收尾 → W2）

1. 金标题从 6 题补到 30 题（6 类 × 5），优先补 R1 与 A1。
2. A1 的三份合成语料（注入 / 单位陷阱 / 矛盾证据）落到 `fixtures/adversarial/`。
3. W2：手写 ReAct + BM25 朴素检索，跑出第一个非零基线。
