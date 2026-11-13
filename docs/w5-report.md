# W5 报告：从终端到服务

日期：2026-11-13 · 对应计划 `m4-w5-plan.md` · 部署：http://47.96.253.123

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
| H 压测/手册 | 20 并发报告；`docs/runbook.md`（部署/回滚/密钥/DB/故障） | `reports/loadtest-*.json`（本地） |

---

## 2. 线上验收

| 项 | 结果 |
|---|---|
| 可访问 | `GET /api/health` → `{"ok":true,"db":true}`；真实问题返回带 `[F#]/[K#]` 的答案 |
| 单题成本 | 例：$0.00047（纯数值）；$0.0026–0.0031（多跳，7 次工具调用） |
| 检索成本 | 已进 `usage.retrieval_cost_usd`；数值题不走检索时为 0（是事实，不是漏记） |
| 数据库 | `chunks=20378`、`facts=85079`、`chunk_vectors=20373` |
| 镜像 | `ghcr.io/evinyang-zw/fintrace:d5bd2fe`，amd64，多阶段，非 root |

### 故障演练（全部在生产环境执行）

| 演练 | 操作 | 结果 |
|---|---|---|
| 进程中断 | 跑到一半 `docker restart app` | 该 run 立即变 `failed / interrupted by restart`，不再永远 running |
| 工具层宕机 | `docker stop mcp` | `/api/health` → `mcp:false`；提问优雅拒答（`insufficient_evidence`），不挂 120s |
| 预算熔断 | 日预算调到 $0.000001 | `/api/ask` → 503 `daily budget exhausted` |
| 回滚 | 切回上一个镜像 tag | 健康检查通过；再切回新版本同样通过 |

> 回滚时发现一次"镜像标签在、层没解压全"的情况（`docker load` 报了 layer NotFound 但容器还能跑）；
> 现在 runbook 要求回滚后必须看 `/api/health` 与一次真实提问，不只看容器状态。

### 20 并发（生产环境，真实模型）

```json
{"concurrency": 20, "total": 20, "ok": 20, "failed": 0, "server_errors": 0,
 "wall_s": 29.0, "p50_ms": 16409, "p95_ms": 28570, "max_ms": 28993}
```

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
`data/traces/*.json`。要做 per-call 成本归因，下一步在 `agent/llm.py` 里加 hook。

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

## 6. 已知边界

- 单实例、`--workers 1`：队列/SSE/限流都在进程内，横向扩展要先换 Redis。
- 无域名/HTTPS：`http://47.96.253.123`，Caddy profile 未启用。
- 门禁是 10 题 smoke，不是统计意义上的全量评测。
- demo token 公开，只靠每日配额与预算兜底。
- 50 题全量尚未在线上跑（本地 W4 数据仍在报告里）。

---

## 7. 下一步（W6）

1. 100 题终版 + holdout，出一份可引用的总报告
2. 域名 + HTTPS（Caddy profile）
3. Langfuse 逐 generation（成本归因到每一步）
4. 两篇文章 + 5 分钟视频 + 面试问答卡
