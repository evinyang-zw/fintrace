<!-- AUTO:SNAPSHOT:START -->
> 单次运行快照（graph · seed 0）。**头条数字是 4 seed 合并，见 [评测报告](../evaluation-report.md)。**
> 由 `make publish-report` 生成，请勿手改。
<!-- AUTO:SNAPSHOT:END -->

# FinTrace 评测报告 · `graph`

- 数据集：`eval/datasets/gold_v3.jsonl`（60 题；可答 52 / 计分 52 / 不可答 8）
- 判官：`llm-v1`
- 生成时间：2026-11-14T22:32:40
- 说明：目标：答案正确率 ↑、引用支撑率 ↑、误拒率 ↓
- judge_kappa：`0.795`

## 总览

| 指标 | 值 | Δ | 95% CI | 说明 |
|---|---|---|---|---|
| 答案正确率 | 98.1% | — | [92.3, 100.0] | 计分题中得分 ≥1 的比例 |
| 平均得分 | 0.91 | — | — | 0–1，rubric 归一化 |
| 引用支撑率 | 96.7% | — | [84.6, 98.1] | claim 级语义支撑，非结构匹配 |
| 召回@8（F2） | 100.0% | — | — | n=15，section 级 |
| 数值准确率 | 100.0% | — | — | C1+C2，容差 0.5% |
| 拒答召回 | 100.0% | — | — | R1 中正确拒答的比例 |
| 误拒率 | 0.0% | — | — | 可答题中被拒的比例 |
| 幻觉率 | 0.0% | — | — | R1 中被强行回答的比例 |
| 未过校验发布 | 2 | — | — | verification_failed=true 的答案数 |
| 单题成本 | $0.0006 | — | — | LLM + 工具 |
| 延迟 p50 / p95 | 4882 / 13072 ms | — | — | 端到端 |

## 分类别

| 类别 | 题数 | 计分 | 作答数 | 正确率 | 引用支撑 |
|---|---|---|---|---|---|
| A1 对抗 | 8 | 8 | 8 | 100.0% | 75.0% |
| C1 计算 | 8 | 8 | 8 | 100.0% | 100.0% |
| C2 对比 | 11 | 11 | 11 | 100.0% | 100.0% |
| F1 事实 | 10 | 10 | 10 | 100.0% | 100.0% |
| F2 多跳 | 15 | 15 | 15 | 93.3% | 86.7% |
| R1 不可答 | 8 | 0 | 0 | —（未计分） | 0.0% |

## 按标签

| 标签 | 题数 | 计分 | 正确率 | 引用支撑 |
|---|---|---|---|---|
| citation_trap | 5 | 5 | 100.0% | 100.0% |

## 失败样本

| id | 类别 | 判官备注 | 回答摘要 |
|---|---|---|---|
| `f2-011` | multi_hop | judge: 汇率影响方向错误：应为3个百分点不利影响，答案称有利影响; citation: 1 supported / 0 partial / 0 unsupported / 1 uncited (score 1.00); unscored in W2 (multi_hop); keypoint coverage=0.81; citations 1/1 | 百事 FY2024 拉美（LatAm）分部：报告口径净营收增长 0.5%，其中汇率折算带来 3 个百分点的有利影响，有机 |
| `r1-001` | unanswerable | refused as required | 无法确认：微软（MSFT）在 as_of 2026-10-01 之前披露的文档中，没有给出 2027 财年总营收的预测或 |
| `r1-002` | unanswerable | refused as required | 无法确认：在 as_of=2025-01-01 之前披露的英伟达（NVDA）文件中，没有 2026 财年全年营收的预测或 |
| `r1-003` | unanswerable | refused as required | 无法确认：工具在 as_of=2025-06-30 下对 TSLA（及 Tesla）均返回 unknown ticker |
| `r1-004` | unanswerable | refused as required | 无法确认：微软财报（10-K/10-Q）中并未披露 Azure 大客户的具体名单，也未披露按客户划分的收入排名或客户集中 |
| `r1-005` | unanswerable | refused as required | 无法确认：在 as_of 2026-10-01 之前披露的苹果公司文件中，检索不到任何“内部邮件”内容，也没有关于下一代 |
| `r1-006` | unanswerable | refused as required | 无法确认：截至 2026-10-01 的披露文件中，没有对 2027 财年 AWS 分部营业收入的预测或指引。财报仅提供 |
| `r1-007` | unanswerable | refused as required | 无法确认：工具对 TSLA 返回 unknown ticker，xbrl_fact 与 search_filings 均 |
| `r1-008` | unanswerable | refused as required | 无法确认：微软的 SEC 披露文件（10-K/10-Q）中不包含董事会会议纪要，也没有披露董事会讨论过的潜在并购标的。检 |

---

> 判官口径见上表。
