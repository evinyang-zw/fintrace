# W5 报告：从终端到服务

日期：2026-11-13 · 审计更新：2026-11-14 · 对应计划 `m4-w5-plan.md` · 部署：http://47.96.253.123

## 一句话结论

**它不是"能跑"，而是"能访问、能计费、能中断、能回滚"。**
线上 20 并发 20/20 成功、0 个 5xx、端到端 p95 28.6s；一次真实 PR 有评测机器人评论；
单题成本含 LLM 与检索；`kill -9` 恢复在 W4 已证明，W5 把它放进了服务。

---

## 1. 交付物

| # | 交付物 | 证据 |
|---|---|---|
| A 服务层 | `api/`：runs 表、队列（6 worker / 50 容量）、SSE、token、限流、日预算 | `tests/test_api.py` 6 条 |
| B 前端 | 单页问答 + SSE + 引用 + 待审操作；`/dashboard` | http://47.96.253.123 |
| C MCP HTTP | `mcp_server --transport http` + 客户端连接池 | `tests/test_mcp_http.py` 3 条；20 并发下工具不排队 |
| D 镜像/编排 | `deploy/Dockerfile`（多阶段、非 root、apt 安全更新）+ `compose.prod.yml`（db/mcp/app） | 线上三容器 healthy |
| E CI 门禁 | 10 题 fixture replay + 提交基线 + 阈值 + PR 评论 | CI job `test`（见 PR 评论） |
| F 部署 | 阿里云 2C4G，`http://47.96.253.123`，数据 20,378 chunk / 85,079 fact / 20,373 向量 | `/api/health` |
| G 观测 | Langfuse Cloud：每次运行一个 span（输入/输出/成本/延迟/状态） | observation `fintrace.run` |
| H 压测/手册 | 20 并发报告（线上 + 本地）；`docs/runbook.md`（部署/回滚/密钥/DB/故障） | 见 §2；`/tmp` 与报告记录 |

---

## 2. 线上验收

| 项 | 结果 |
|---|---|
| 可访问 | `GET /api/health` → `{"ok":true,"db":true,"mcp":true,"llm":true}`；真实问题返回带 `[F#]/[K#]` 的答案 |
| 单题成本 | 7 日均值 $0.00129；纯数值题约 $0.0005，多跳题 $0.0026–0.0031 |
| 检索成本 | 已进 `usage.retrieval_cost_usd`；数值题不走检索时为 0（是事实，不是漏记） |
| 数据库 | `chunks=20378`、`facts=85079`、`chunk_vectors=20373` |
| 镜像 | `ghcr.io/evinyang-zw/fintrace:0cc353d`，amd64，多阶段，非 root；302MB（目标 <500MB） |

### 故障演练（全部在生产环境执行）

| 演练 | 操作 | 结果 |
|---|---|---|
| 进程中断 | 跑到一半 `docker restart app` | 该 run 立即变 `failed / interrupted by restart`，不再永远 running |
| 工具层宕机 | `docker stop mcp` | `/api/health` → `mcp:false`；提问优雅拒答（`insufficient_evidence`），不挂 120s |
| 预算熔断 | 日预算调到 $0.000001 | `/api/ask` → 503 `daily budget exhausted` |
| 回滚 | 切回上一个镜像 tag | 健康检查通过；再切回新版本同样通过 |

> 回滚时发现一次"镜像标签在、层没解压全"的情况（`docker load` 报了 layer NotFound 但容器还能跑）；
> 现在 runbook 要求回滚后必须看 `/api/health` 与一次真实提问，不只看容器状态。

### 20 并发（最终镜像，真实模型）

线上（`0cc353d`）：

```json
{"concurrency": 20, "total": 20, "ok": 20, "failed": 0, "server_errors": 0,
 "wall_s": 28.5, "p50_ms": 17036, "p95_ms": 26828, "max_ms": 28486}
```

本地 compose（同一套 API，20/20，p95 29.0s；本地镜像是审计前的 arm64 构建）：

```json
{"concurrency": 20, "total": 20, "ok": 20, "failed": 0, "server_errors": 0,
 "wall_s": 29.5, "p50_ms": 17257, "p95_ms": 28957, "max_ms": 29472}
```

补充测量（线上 trace / runs 表）：队列等待 p95 **16.2s**（<30s 目标）；
工具延迟（不含 embedding/rerank）compute p95 20ms、get_section 43ms、xbrl_fact 333ms
（后者是 DB 模糊匹配本身，不是 MCP 往返；MCP 往返 W4 实测 <50ms），search p95 538ms
（其中约 340ms 是 embedding API）。

> 第一次跑得到 17 个 429：**限流在工作，不是故障**。压测前把 `API_RATE_PER_MIN`
> 临时调到 200，跑完恢复为 5；这条已写进 runbook。

---

## 3. 评测门禁

- 数据集：`eval/datasets/smoke.jsonl`（10 题，全部来自可复现的合成语料）
- 录制：agent + 判官的全部调用进 `tests/cassettes`（约 150 个文件、1.3MB）
- 基线：`eval/baselines/smoke.json`
- 阈值：引用支撑 −2pt 阻断、误拒率 +5pt 阻断、正确率 −5pt 警告、成本 +30% 警告
- **replay 条数必须等于基线条数**：少一条就说明 cassette miss，直接阻断，不许"少跑几题也算过"

为什么是 10 题而不是 50 题：CI 里没有 464MB 的完整仓库，只有 5 份合成文档可复现。
50 题全量 replay 需要完整 dump，留给夜间任务；PR 门禁只保证"轨迹没被改坏"。

---

## 4. Langfuse

每次运行一个 span：`input=question`、`output=answer`、`metadata={cost, retrieval_cost,
model, tool_calls, latency_ms, status, error}`。
本地与线上都已实测：部署 `014fdc1` 后的一次真实提问在 2026-11-13 08:39 UTC 产生
`fintrace.run` observation。写入失败只 warning，不影响用户请求。

**限制**：只有 run 级 span，没有逐次 LLM generation；逐步骤信息仍在
`data/traces/*.json`。计划 §9.1 的"在 llm.py 包 generation span"未做，原因是现有
HTTP 客户端不在 OTel 上下文里执行；下一步在 `agent/llm.py` 加显式 `trace_context` hook。
SDK 为 5.2.0（计划写 v4/OTLP，v5 同样走 OTLP），实现用显式 `host=` 参数，
服务器 env 为 `LANGFUSE_HOST`（不是计划里的 `LANGFUSE_BASE_URL`）。

---

## 5. 踩到的坑（都是真坑）

1. **Mac 构建 arm64，服务器 amd64** → `exec format error`。手动构建必须
   `--platform linux/amd64`；`docker save` 在 Docker Desktop 的 containerd store 上
   对按平台拉取的镜像导出为空，最终用 `crane pull --platform` 导出。
2. **服务器拉不到 Docker Hub**（`registry-1.docker.io` 超时）→ 用
   `docker.m.daocloud.io` 镜像再 tag 回原名。
3. **GHCR 在服务器上约 15KB/s**，自动部署 15 分钟超时 → release 改为"构建 job 上传
   镜像 artifact，部署 job scp 到服务器"，不再在服务器上 pull。
4. **env 文件被弄丢过一次**：scp 的 `.env` 只剩追加的 4 行，app 启动报
   `LLM_API_KEY is empty`。runbook 增加了"改 env 后先 `docker compose config` 检查"。
5. **MCP 的 Host 校验**：容器里 Host 是 `mcp:8001`，SDK 默认只允许 localhost，全部
   421。加 `MCP_ALLOWED_HOSTS`。
6. **限流默认信任 `X-Forwarded-For`**：任何人都能伪造 IP 绕过限额。改成只有
   `TRUST_PROXY=1` 才读；本轮已修。
7. **检索成本恒为 0**：计数器加在 `EvidenceStore` 上但没进 state，每个节点重建后清零。
   加进 `GraphState` 并在 `state_evidence` 持久化后才对。

---

## 6. W5 计划审计（2026-11-14）

按 `m4-w5-plan.md` 逐条核对，不是看报告自述。**8 个交付物全部落地；审计发现 9 处问题，
8 处已修，1 处受 CI 数据约束保留并记录。**

| 计划项 | 状态 | 证据 |
|---|---|---|
| A 服务层（runs/队列/SSE/鉴权/限流/预算） | ✅ | `api/`、`tests/test_api.py` 6 条 |
| B 前端 + 待审 | ✅ | `/`、`/dashboard`；API 复核流程已测 |
| C MCP HTTP + 连接池 | ✅ | `tests/test_mcp_http.py` 3 条 |
| D 镜像 / compose | ✅ | `compose.prod.yml`；镜像 **302MB**；`make prod-up` |
| E CI 门禁 + PR 评论 | ✅ | PR [#2](https://github.com/evinyang-zw/fintrace/pull/2)；门禁决策 8 条单测 |
| F 部署 + 数据恢复 | ✅ | URL、`/api/health`、30 天 64 次运行（63 done / 1 failed）、`chunks=20378` |
| G 观测 + 成本 | ✅ | Langfuse `fintrace.run`；`by_tier` 已出现在 `/api/stats` |
| H 压测 + runbook + 演练 | ✅ | 线上/本地 20 并发；四项演练全过 |

### 6.1 审计发现与修复

| # | 问题 | 严重度 | 处理 |
|---|---|---|---|
| F1 | **MCP 边界丢检索元数据**：server 在自己的 store 里记 `search_calls`/usage，返回时丢掉；线上 `Answer.retrieval` 恒为空，`retrieval_cost` 恒为 0，按档位看板无数据 | 高 | 已修：payload 带回 `search_calls` + `usage`，客户端恢复；`tests/test_mcp_payload.py` |
| F2 | **待审两张表**：graph 仍写 W4 `review_queue`，API 读 `runs` | 中 | 已修：停止写旧表，Streamlit 改读 `runs.status='awaiting_review'` |
| F3 | `/api/health` 缺 `llm` 字段 | 低 | 已修：返回 key 是否配置（不做真实调用） |
| F4 | `/dashboard` 缺"按检索档位" | 中 | 已修：`by_tier`（成本/延迟/运行数）+ 页面表格 |
| F5 | 镜像 814MB，超 §7.1 的 <500MB 目标 | 中 | 已修：runtime 换 `python:3.12-slim`、`COPY --chown` 去掉 200MB 重复层、排除 tests/fixtures → **302MB** |
| F6 | README 缺架构图 | 低 | 已修：加 ASCII 架构与进程边界 |
| F7 | 门禁阻断逻辑不可单测 | 中 | 已修：抽 `eval/gate.py`，8 条单测覆盖 n 缺失/引用/误拒/正确率/成本 |
| F8 | 门禁 10 题而非计划 §8.2 的 50 题 | 中 | **未修**：CI 没有 464MB 仓库数据，只有 5 份合成文档可复现；50 题 replay 需要完整 dump（W6 夜间任务） |
| F9 | 镜像扫描 1 个 HIGH（soupsieve 3.0.4） | 中 | 已修：升到 4.1，扫描通过 |

### 6.2 仍然存在的偏差（不是缺陷，是取舍）

- **Langfuse**：只有 run 级 span，没有逐 LLM generation（见 §4）。
- **看板**：`hedged / hedge_won` 没进 `runs` 表，看板看不到（trace 里有）。
- **50 题全量**：线上从未跑过；W4 的 50 题数字仍是本地实验。
- **域名/HTTPS**：Caddy profile 未启用，按计划先 IP:port。
- **DEMO_TOKEN**：没有写进 README（避免仓库里出现可用 token），与计划 §5.4 略有出入。
- **cassette**：未 gzip；当前 150 个文件 1.3MB，可接受。
- **演示 GIF**：按裁剪顺序延到 W6（计划允许，需在此写明）。
- **工具 p95**：`xbrl_fact` 333ms 略高于 §6 的 300ms 目标；MCP 往返本身 <50ms。

### 6.3 审计后的验证

- CI：202 tests、覆盖率 **76.22%**、lint/mypy 全绿；门禁在 PR #2 上发评论并通过。
- 部署：`release` 构建（含 trivy + SBOM + 302MB 镜像）→ artifact → 服务器，自动部署成功；
  生产运行 `0cc353d`。
- 线上：`health` 四项全 true；检索题返回 `tiers=['dense',...]`、`retrieval_cost=4.27e-06`；
  `/api/stats` 的 `by_tier` 有数据。

## 7. 已知边界

- 单实例、`--workers 1`：队列/SSE/限流都在进程内，横向扩展要先换 Redis。
- 无域名/HTTPS：`http://47.96.253.123`，Caddy profile 未启用。
- 门禁是 10 题 smoke，不是统计意义上的全量评测。
- demo token 只在服务器 `.env`，不写进仓库；公开演示靠每日配额与预算兜底。
- 50 题全量尚未在线上跑（本地 W4 数据仍在报告里）。
- 自动部署依赖 GitHub artifact 传到服务器；GHCR 直拉在服务器上不可用（见 runbook）。

---

## 8. 下一步（W6）

1. 100 题终版 + holdout，出一份可引用的总报告
2. 域名 + HTTPS（Caddy profile）
3. Langfuse 逐 generation（成本归因到每一步）
4. 两篇文章 + 5 分钟视频 + 面试问答卡
