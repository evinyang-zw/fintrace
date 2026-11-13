# 人工标注（盲标）

用于校准判官与引用校验器。导出标注包时隐藏机器分，人工先独立判；只改 rubric，不改人标。

| 文件 | 条数 | 用途 | 复算 |
|---|---|---|---|
| `judge-labels.csv` | 60 | 判官 correctness 0/1/2 | `make kappa` |
| `citation-labels.csv` | 30 | claim 级引用支撑 supported/partial/unsupported | `make citation-kappa` |

列：

- `judge-labels.csv`：`sample_id, question_id, category, question, key_points, gold, answer, label, reason`
- `citation-labels.csv`：`sample_id, question_id, claim_index, category, claim, spans, label, reason`

本地工作路径是 `data/annotation/`；这里是公开副本，内容一致。
