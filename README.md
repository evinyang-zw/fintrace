# FinTrace

> **可审计的财报研究代理**：每一句话都有出处，每一个数字都能复算。
> 中文提问 → 在美股公开财报里检索、计算、交叉验证 → 输出逐句引用的研究简报；证据不足时明确拒答。

[![ci](https://github.com/evinyang-zw/fintrace/actions/workflows/ci.yml/badge.svg)](https://github.com/evinyang-zw/fintrace/actions/workflows/ci.yml)
[在线 Demo](http://47.96.253.123) · [评测报告](docs/evaluation-report.md) · [5 分钟演示脚本](docs/demo-script-w6.md) · [架构决策](docs/adr/)

## 结果

70 题金标 = **60 道开发集 + 10 道一次性 holdout**（6 类，每类 ≥8）。开发集跑 4 个 seed。

| 指标 | W4（50 题，4 seed） | W6 同题 50 子集 | **W6 开发集（60 题 × 4 seed）** | W6 holdout（10 题 × 2） |
|---|---|---|---|---|
| 答案正确率 | 95.0% | 98.9% `[97.2, 100]` | **99.0%** `[97.6, 100]` | **95.0%** `[85.0, 100.0]` |
| 引用语义支撑率 | 94.4% | 98.1% | **98.4%** | 91.8% |
| 数值准确率 | — | 98.3% | 98.7% | 100% |
| 误拒率 | ~5% | 0.0% | **0.0%** | 5.0% |
| 单题成本 | $0.00065 | $0.00060 | **$0.00059** | $0.00103 |
| p95 延迟 | 13.0–14.3s | 13.1s | **12.6s** | 14.4s |

- 50 子集与 W4 **同题可对照**；60 开发集题变了，只能描述；holdout 只跑过一次。
- W6 相对 W4 只改了工具与判分器（XBRL 财年元数据、别名、比率容差），agent 编排未变。
- holdout 唯一失败：AMZN 分部表的一题在一次运行里被拒答——**表格类多跳仍是弱点**。
- 完整口径：[评测报告](docs/evaluation-report.md) · 指标定义：[metrics.md](docs/metrics.md) §11。

## 它解决什么

普通"财报 RAG"会在三个地方翻车，这个项目逐条对着做：

1. **数字错**（把百万读成十亿、拿错财年）→ 数字不经过 LLM：所有财务数字来自 SEC XBRL 结构化数据，模型只负责选事实和措辞，校验器用 `[F#]` 事实做确定性复算。
2. **引用假**（引了一段相关但不支撑的段落）→ 引用粒度为字符区间 `[K#]`，claim 级语义校验；判官用 60 条盲标校准，加权 κ=0.795。
3. **该拒却编**（问财报里没有的东西）→ 8 道不可答题全部正确拒答；单题预算与工具超时保证它"优雅地不知道"。

## 在线 Demo

**http://47.96.253.123** —— 阿里云 2C4G、单实例 Docker Compose。

- 演示需要口令（防滥用，每日配额与预算兜底）：**联系我获取**；`/dashboard` 只读，无需口令。
- 5 分钟演示：录屏脚本见 [`docs/demo-script-w6.md`](docs/demo-script-w6.md)（成片待录，W6 唯一未完成的交付物）。
- 服务层：FastAPI + 队列（6 worker / 50 容量）+ SSE + 人工待审；app 与 MCP 工具是两个容器。

## 架构

```
浏览器 ──HTTP / SSE──┐
                     ▼
              app  (FastAPI, :8000)
              ├── runs 表 / 队列 / SSE / token / 限流 / 日预算
              ├── LangGraph agent（agent → tools → verify → repair → review → finalize）
              │        └── MCP over HTTP ──► mcp (:8001, 只读) ──► Postgres + pgvector
              └── Langfuse（每次运行一个 trace span）
```

LangGraph 的图是**编排骨架**，不是固定流水线：内层仍由模型自己决定查哪一节、取哪个 tag；
`verify → repair` 是 W4 新增的生成时校验，`review` 是人工中断（Postgres checkpoint，换进程可恢复）。

## 三个关键决策

- **先写评测，再写 Agent**（[ADR-001](docs/adr/ADR-001-eval-first.md)）：没有基线不动手；NullAgent 先证明尺子有效。
- **手写 ReAct，再迁到图**（[ADR-002](docs/adr/ADR-002-handwritten-react.md) / [ADR-006](docs/adr/ADR-006-graph-port.md)）：换图 A/B 打平（95.0% vs 95.0%），买到的是跨进程恢复、人工中断、生成时校验。
- **工具走 MCP 独立进程**（[ADR-007](docs/adr/ADR-007-mcp-boundary.md)）：服务端无状态、只读、5s 超时；句柄由客户端分配，接口等价测试保证"换进程边界不改变行为"。

## 快速开始

```bash
uv sync && make up          # 依赖 + Postgres(pgvector)
make ingest                 # 10 家公司 / 233 份 filing（首次约 10 分钟；之后走快照）
make embed                  # 2 万 chunk 向量（需要 DASHSCOPE_API_KEY；hybrid 检索用）
make q-check                # 70 题数据校验（schema + XBRL 对账 + 引用存在性）
make eval AGENT=graph JUDGE=llm            # 开发集 60 题（默认 gold_v3）
make eval AGENT=graph JUDGE=llm ARGS="--tags holdout"   # 一次性 holdout
```

完整复现（4 seed + 合并表）见[评测报告 §7](docs/evaluation-report.md)；环境变量见 `.env.example`。

## 评测怎么做的

- **两段 A/B，一次只改一个变量**：先固定工具边界只换图，再固定图只换工具边界。
- **判官先校准再使用**：60 条盲标、人标不动只改 rubric，κ 0.603 → 0.795；引用 checker 30 条 claim，κ=0.634。
- **配对 bootstrap**：所有差异给 95% CI，不报两个均值相减。
- **holdout 纪律**：10 道题写完只做数据校验，开发集稳定、打 tag 后才跑一次；第一次跑出来的"40% 数值准确率"是**判分器 bug**，修的是尺子不是模型，作废重跑并写进报告。
- **CI 门禁**：每个 PR replay 10 题（无网络、无 API key），引用支撑 −2pt / 误拒 +5pt 阻断，机器人发评论。

## 限制

- **holdout 只有 10 题**，只能看方向；开发集 99% 是开发集，不是分布外准确率。
- **表格仍是弱点**：holdout 唯一失败与 W4 的 C2 掉分都集中在表格式口径。
- **单实例**：队列/SSE/限流在进程内，横向扩展要先换 Redis；无 HTTPS（大陆节点 80/443 需备案）。
- 判官 κ=0.795、引用 checker κ=0.634：相对趋势可信，单题绝对值不宜当精确测量。
- 线上只做 smoke 与并发压测，70 题评测在本地跑。

完整限制与失败案例见[评测报告 §6](docs/evaluation-report.md) 与[两篇文章](docs/articles/)。

## 工程

- **208 tests**、覆盖率 **76%**、ruff + mypy strict、CI 不依赖网络（cassette 回放）。
- 线上：**20 并发 20/20 成功，0 个 5xx**，端到端 p95 28.6s，队列等待 p95 16.2s；30 天 64 次运行 63 done / 1 failed。
- 镜像 302MB、非 root、trivy HIGH/CRITICAL 阻断；部署/回滚/密钥轮换/DB 恢复见 [runbook](docs/runbook.md)。

## 阶段记录

| 周 | 做了什么 | 关键数字 | 报告 |
|---|---|---|---|
| W1 | 数据摄取 + 评测协议 + NullAgent | 10 家 / 233 filing / 85,079 facts；30 题 | [w1](docs/w1-report.md) |
| W2 | 手写 ReAct + 朴素 RAG 基线 | 20 计分题 95%，但 F2 多跳实质 0/5 | [w2](docs/w2-report.md) |
| W3 | 语义判官 + hybrid 检索 + claim 级引用 | 45 题 95–97.5%，引用 56.1%→95%+ | [w3](docs/w3-report.md) |
| W4 | LangGraph + verify/repair + MCP + HITL | 50 题 A/B 95/95，跨进程恢复 | [w4](docs/w4-report.md) |
| W5 | FastAPI 服务 + 部署 + CI 门禁 + Langfuse | 20 并发 0 个 5xx，线上可访问 | [w5](docs/w5-report.md) |
| W6 | 70 题终版 + 文档/文章/面试材料 | 开发集 99.0%，holdout 95.0% | [评测报告](docs/evaluation-report.md) |

## 三条不可让步的规则

1. **数字不经过 LLM**：所有财务数字来自 XBRL，正文只负责定性叙述。
2. **引用到字符区间**：引用不是"某份文档"，而是 `(accession, item, char_start, char_end)`。
3. **评测先行**：没有基线分数之前，不写 Agent。

## 目录

```
pipelines/      离线摄取：EDGAR → section/chunk/facts → Postgres
agent/          图编排、工具、检索、MCP 客户端、trace
mcp_server/     4 个只读工具（独立进程，streamable HTTP）
api/            FastAPI 服务、队列/SSE/鉴权/限流/预算
eval/           问题 schema、判官、citation checker、指标、CI gate
web/            单页前端 + trace 回看
docs/           评测报告、ADR、runbook、文章、各周报告
deploy/         Dockerfile、compose.prod.yml
tests/          208 个测试（CI 不联网）
```
