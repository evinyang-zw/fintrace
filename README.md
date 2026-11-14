# FinTrace

> 可审计的财报研究代理：**每一句话都有出处，每一个数字都能复算。**

用中文提问，在美国上市公司公开财报里检索、计算、交叉验证，输出带逐句引用的研究简报；
证据不足时明确拒答并说明缺什么。

这不是一个"能聊财报的机器人"。它是一个**带证据链、可评测、可运维**的 Agent 系统，
仓库里最重要的部分不是 Agent 代码，而是**用来证明 Agent 好不好的那套尺子**。

## 状态

| 模块 | 状态 |
|---|---|
| 数据摄取（EDGAR 10-K/10-Q + XBRL） | W1 完成 |
| 评测协议 / NullAgent / 指标 / bootstrap | W1 完成 |
| 金标题（50 题：F2 15、C2 10、F1 10 含 5 道引用陷阱） | W1 30 → W3 45 → W4 50 |
| 检索（BM25 / dense / RRF / rerank） | W3 实现三档，可显式降级 |
| 引用（结构 → claim 级语义） | W3：数字引 `F#`，正文引 `K#` |
| 语义判官（LLM + 缓存 + 自一致性） | W3 实现；60 条 κ 校准待做 |
| Agent 编排 | W2 手写 ReAct；W4 LangGraph 图（checkpoint / interrupt / verify-repair） |
| trace 回看 / 离线回放 | W2 完成，W3 记录检索档位与降级原因 |
| MCP 工具服务 | W4：4 工具独立进程，stdlib + 契约测试 |
| CI（GitHub Actions，含 Postgres service） | W1 完成，W2 加 fixtures 冒烟 |
| 生产化（镜像 / 部署 / 监控） | **W5 完成**：http://47.96.253.123 |
| HITL（中断 / 恢复 / 人工放行） | W4：Postgres checkpoint，跨进程恢复 |

## 在线 Demo（W5）

**http://47.96.253.123** —— 单实例、Docker Compose、阿里云 2C4G。

- 首页：提问、SSE 进度、引用可展开、待审可放行
- `/dashboard`：请求量、成功率、单题成本、p50/p95、缓存命中
- `/api/health`：DB / 队列深度 / 当日预算
- 访问令牌不写进仓库；公开演示用服务器上的 `DEMO_TOKEN`（限次 + 每日预算兜底）

服务层是 FastAPI（`api/`）：`POST /api/ask` 入队返回 202，`GET /api/runs/{id}/events`
推 SSE，`/api/runs/{id}/review` 恢复人工中断。app 与 MCP 工具是两个容器，
工具通过 streamable HTTP 调用，服务端无状态、只读、句柄由客户端分配。

```bash
make api                      # 本地起服务（:8000）
make gate                     # replay 评测门禁（对比 eval/baselines/smoke.json）
make db-dump                  # 导出仓库（服务器恢复用）
```

## 架构（W5）

```
浏览器 ──HTTP / SSE──┐
                     ▼
              app  (FastAPI, :8000)
              ├── runs 表 / 队列（6 worker）/ SSE / token / 限流 / 日预算
              ├── LangGraph agent（verify → repair → review）
              │        └── MCP over HTTP ──► mcp (:8001, 只读) ──► Postgres + pgvector
              └── Langfuse（每次运行一个 span）
```

- app 与 mcp 是同一镜像的两个入口；数据库独立容器，数据用 dump/restore 搬。
- 队列、SSE、限流都在 app 进程内，所以 `--workers 1`；要横向扩展先换 Redis。
- 部署与回滚见 [docs/runbook.md](docs/runbook.md)。

## W3 检索栈（2026-10-30）

`chunk_vectors`（独立表，带模型名）+ 三档检索，缺 key 或缺向量时**显式降级**：

| 档 | 组成 | 用途 |
|---|---|---|
| `bm25` | 词法检索 | 基线；无外部依赖 |
| `hybrid` | BM25 + dense，RRF（k=60） | 跨语言召回 |
| `hybrid_rerank` | hybrid 20 候选 → rerank → 8 | 排序质量 |

```bash
make embed-smoke   # 15 秒验证百炼 embedding（维度/相似度）
make rerank-smoke  # 15 秒验证 rerank（相关段落必须排第一）
make embed         # 回填 2 万 chunk，一次性
RETRIEVAL_TIER=hybrid make eval AGENT=react JUDGE=llm
make ablate        # 三档 × 2 seed 的消融（会写 reports/ablation/）
```

数字引用 `[F#]`、正文引用 `[K#]`：前者用 XBRL fact 做确定性比对，后者用 LLM 判
"这段是否支撑这句话"。见 `docs/adr/ADR-004-fact-citations.md`。

**判官已校准**：60 条盲标，二次加权 κ = **0.795**（rubric v3.1，完全一致 90%）。

**W3.1 结果**（45 题）：修复"只读 400 字摘要"后，F2 从约 50% 升到 100%（hybrid），
总正确率 77.5% → 95.0%（seed 0）/ 100%（seed 1），配对差异 +17.5pt `[+5.0, +32.5]`。
瓶颈不是召回，是"看到证据却不去读全文"。

**最终消融**（当前代码，3 档 × 2 seed）：hybrid 95.0/97.5%，bm25 92.5/95.0%，
rerank 92.5/97.5%；档位差异仍在噪声内。判官 60 条盲标加权 κ = 0.795，
判官自一致性 100%（20 条重跑）；引用校验器 30 条 claim 加权 κ = 0.634。完整表格见
[docs/w3-report.md](docs/w3-report.md) §11。

## W4 图编排（2026-11-11）

手写 ReAct 换成了 LangGraph 图：`agent → tools → verify → repair → review → finalize`。
内层推理不变（ADR-006），新增的是状态机、Postgres checkpoint、人工中断和生成时校验。

**A/B（50 题 × 4 seed，两个 Agent 同为 9 步预算，LLM 判官）**

| | react | graph |
|---|---|---|
| 正确率（n=180 配对） | 95.0% | **95.0%**，Δ CI `[-3.3, +3.9]` |
| 引用支撑率 | 90.9% | **94.4%** |
| F2 多跳 | 85.0% | **90.0%** |
| 单题成本 / p95 | $0.00063 / ~13s | $0.00065 / ~14s |

MCP 版本（工具跑在独立进程）与进程内版能力一致；8 条契约测试覆盖 schema、坏参数、超时和只读。
工具描述实验：现行描述 17/20 选对，极简描述 15/20——描述是接口的一部分。

```bash
make graph-ask Q="苹果 FY2024 营收？"              # 图版单问
make graph-ask Q="..." ARGS="--review"            # 先中断，人工审
make resume THREAD=... ACTION=approve             # 新进程恢复
FINTRACE_TOOL_BACKEND=mcp make eval AGENT=graph   # 工具走 MCP
make compare A=reports/a.json B=reports/b.json    # 配对比较
```

完整数据与失败分析见 [docs/w4-report.md](docs/w4-report.md)。

## W2 基线（2026-10-29）

手写 ReAct + BM25 + XBRL 工具，30 题金标、两次独立运行：

| 指标 | seed 0 | seed 1 |
|---|---|---|
| 答案正确率（20 计分题） | 95.0% | 100% |
| 引用支撑率 | 92.0% | 96.0% |
| 数值准确率 | 90.0% | 100% |
| 拒答召回 / 误拒率 | 100% / 8% | 100% / 4% |
| 单题成本 / p95 延迟 | $0.0010 / 9.2s | $0.0010 / 11.1s |

`FINTRACE_LLM_MODE=replay` 跑完整 30 题可**逐项复现**上述指标（不联网、0 成本）。

**但计分集合是 XBRL 可直接解决的题**：20 道里 15 道有结构化数字。
真正的短板是检索类（F2 多跳）——5 题里 2 题拒答、3 题关键点覆盖 0.48–0.61，实质 0/5。
W3 的重点因此是语义判官 + 混合检索，而不是继续刷总分。详见 `docs/w2-report.md`。

## W1 验收结果（2026-10-27）

| 项 | 结果 |
|---|---|
| 公司 / filing | 10 / 233 |
| section 覆盖率 | 100%（KO 的 10-Q/A 修正案只有单节，属正常） |
| XBRL facts | 85,079 行 |
| chunks（朴素切块） | 20,373 |
| 金标题 | 30 题（6 类 × 5），15 道数值题与 XBRL 逐条对账 |
| 对抗语料 | 5 份合成文档：注入 / 单位陷阱 / 重述时点 / 实体混淆 |
| NullAgent 基线 | 正确率 0%、引用支撑 0%、误拒率 100%——与预期一致，说明尺子本身没坏 |

## 演示

![W1 demo](docs/demo.gif)

20 秒真实终端录制（`agg` 压掉了等待时间）：`make stats` → `make ingest`（幂等跳过）
→ `make q-check`（30 题 + XBRL 对账）→ `make eval`（零基线）。
可重录：`bash demo/record.sh`。

## 持续集成

[![ci](https://github.com/evinyang-zw/fintrace/actions/workflows/ci.yml/badge.svg)](https://github.com/evinyang-zw/fintrace/actions/workflows/ci.yml)

`.github/workflows/ci.yml`：ruff → mypy → pytest（带 pgvector service）→ eval smoke。
冷启动 32 秒，全绿。

## 证据索引（看什么、在哪看）

| 想看什么 | 去哪 |
|---|---|
| 评测结论（简历里的「评测报告」） | [`docs/evaluation-report.md`](docs/evaluation-report.md) |
| 各阶段过程与失败分析 | [`docs/w4-report.md`](docs/w4-report.md) · [`docs/w3-report.md`](docs/w3-report.md) · [`docs/w2-report.md`](docs/w2-report.md) · [`docs/w1-report.md`](docs/w1-report.md) |
| 指标口径与边界 | [`docs/metrics.md`](docs/metrics.md) |
| 一次原始运行 | [`docs/evidence/latest-run.md`](docs/evidence/latest-run.md) |
| 人工标注（60 + 30 条盲标） | [`docs/evidence/annotations/`](docs/evidence/annotations/) |
| 架构决策 | [`docs/adr/`](docs/adr/) |

**不在 Git 里**：`data/`（572MB 原始 filing 与 trace）、`reports/`（本地运行产物）。
复现命令见评测报告 §6；`make publish-report` 会把最新一次运行快照进 `docs/evidence/`。

## 快速开始

```bash
uv sync                 # 安装依赖
make up                 # 启动 Postgres（pgvector）
make universe           # ticker -> CIK（不手抄十位数字）
make stats              # 仓库存量：filing/section/facts/覆盖率
make ingest             # 抓取并入库（首次约 10 分钟，之后走本地快照）
make ingest-fixtures    # 对抗语料入库（A1 五题的前置）
make q-check            # 校验金标题（含数值对账）
make ask Q="苹果 FY2024 总营收？" UNIVERSE=AAPL   # 单题调试，打印 trace
make eval AGENT=react               # 跑基线（默认 deterministic 判官）
make trace              # Streamlit 回看某次运行（引用可高亮原文）
make test               # 单元测试（含 3 题离线回放；不联网）
```

环境变量见 `.env.example`。SEC 要求请求头里带可联系的 User-Agent。

> 国内网络拉不到 Docker Hub 时，可先 `docker pull <mirror>/pgvector/pgvector:pg17`
> 再 `docker tag` 成本地镜像；`docker-compose.yml` 本身不需要改。

## 结构

```
pipelines/            离线摄取：EDGAR -> 纯文本 -> section/chunk -> Postgres
  sources/            DataSource 协议 + EDGAR 实现（A 股 adapter 从这里插入）
  fixtures.py         对抗语料入库（与真实语料同一套解析器）
agent/                手写 ReAct：llm / tools / prompts / react / trace / pricing
eval/                 评测：问题 schema、agent/judge 协议、指标、确定性判分、报告
  agents/null.py      永远拒答的 NullAgent（用来验尺子，不是用来答题）
web/trace_app.py      Streamlit 回看页：时间线 + 引用高亮
tests/                单元测试 + 契约测试（CI 不联网）
```

关键设计取舍记录在 `docs/adr/`，指标口径见 `docs/metrics.md`。

## 三条不可让步的规则

1. **数字不经过 LLM**：所有财务数字来自 XBRL，正文只负责定性叙述。
2. **引用到字符区间**：引用不是"某份文档"，而是 `(accession, item, char_start, char_end)`。
3. **评测先行**：没有基线分数之前，不写 Agent。
