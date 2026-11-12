# W4 报告：状态机、校验回环、MCP 与人工放行

日期：2026-11-11 · 对应计划 `m3-w4-plan.md` · 代码仓 `fintrace`（W4 分支）

## 一句话结论

**图版和手写版打平**（4 seed × 50 题：95.0% vs 95.0%，配对 CI `[-3.3, +3.9]`），同时拿到三样
手写版没有的东西：跨进程恢复、人工中断、生成时校验。代价是 p95 多约 0.6 秒、代码多约 500 行。

---

## 1. 交付物

| # | 交付物 | 状态 | 证据 |
|---|---|---|---|
| A | LangGraph 图 + Postgres checkpointer | ✅ | `agent/graph/`；同一 thread 换进程可恢复 |
| B | 校验回环（数字/引用/政策） | ✅ | `verify`/`repair` 节点；坏句柄、错数字、注入、过度断言各有单测 |
| C | HITL 中断 / 恢复 | ✅ | `interrupt()` + `Command(resume)`；CLI + Streamlit 待审队列 |
| D | MCP 工具服务 | ✅ | `mcp_server/`（4 工具）+ `MCPBackend`；12 条契约测试 |
| E | 50 题金标 | ✅ | `eval/datasets/gold_v2.jsonl`；`q-check` 全过 |
| F | A/B 回归 | ✅ | 4 seed × 2 agent；`make compare-agents` 出配对区间 |
| G | 文档 | ✅ | ADR-006/007、ADR-002 增补、本报告 |

测试：**185 passed**，覆盖率 **76.1%**，ruff / mypy strict 全绿。

## 2. A/B：换图是否掉分

两段比较，一次只改一个变量。第一段固定"工具在进程内"，只换编排；第二段固定图版，只换工具边界。

### 2.1 换编排（50 题，`AGENT_MAX_STEPS=9`，LLM 判官）

| seed | react | graph |
|---|---|---|
| 0 | 97.8% | 97.8% |
| 1 | 97.8% | 93.3% |
| 2 | 93.3% | 93.3% |
| 3 | 91.1% | 95.6% |
| **4 seed 合并（n=180）** | **95.0%** | **95.0%** |

配对 bootstrap（n=180）：正确率差 **0.0pt**，95% CI `[-3.3, +3.9]` → **未退化**。
（验收线是"下界 ≥ −5pt"。）

分类别（4 seed 合并）：

| 类别 | react | graph | Δ CI |
|---|---|---|---|
| F1 事实（含 5 道引用陷阱） | 100% | 100% | [0, 0] |
| F2 多跳 | 85.0% | **90.0%** | [−3.3, +13.3] |
| C1 计算 | 100% | 100% | [0, 0] |
| C2 对比 | 100% | 92.5% | [−17.5, 0] |
| A1 对抗 | 100% | 100% | [0, 0] |

C2 的差值全部来自 c2-004（WMT 用 net sales 还是 total revenue），两个 Agent 都会偶尔选错，
属于题目口径歧义，不是编排差异。

**F2 的"≥13/15"按单 seed 看不稳定**：graph 四个 seed 是 15、13、12、14，react 是 14、14、12、11。
合并后 graph 90%（约 13.5/15）高于 react 85%。所以 F2 的结论必须按合并样本说，按单次跑会得出相反结论。

### 2.2 换工具边界（同 50 题，deterministic 判官，两个 seed）

| backend | seed 0 | seed 1 | 合并（n=60） |
|---|---|---|---|
| inproc | 96.7%（29/30） | 100%（30/30） | 98.3% |
| mcp | 100%（30/30） | 93.3%（28/30） | 96.7% |

配对差 **−1.7pt**，95% CI `[−6.7, +3.3]`，跨越 0。差异只出现在 c2-004 与 c2-010 两道已知口径
歧义题，不是系统性回退。成本 $0.00060–$0.00135/题、p95 12.3–14.9s，两档互有高低，主要由
provider 抖动与步数决定；单次工具往返实测 <50ms。

比多跑几个 seed 更硬的证据是**接口等价测试**（`tests/test_mcp_equivalence.py`）：同一仓库、
同一检索档位、同一组参数下，进程内与 MCP 返回的 observation **逐字节一致**（覆盖成功调用与
工具级错误）；MCP 协议层的参数校验错误被客户端映射成同一个 `bad_args` 码，消息仍点名缺失字段。

### 2.3 成本与延迟

| | react | graph |
|---|---|---|
| 单题成本（4 seed 均值） | $0.00063 | $0.00065 |
| p50 | 5.5–5.9s | 5.4–5.9s |
| p95 | 12.6–13.8s | 13.0–14.3s |

校验节点每答案多一次 LLM 调用，成本增加约 3%；p95 增加约 0.6s。

## 3. 校验回环做了什么

`verify` 三查：

| 检查 | 规则 | 结果 |
|---|---|---|
| 数字 | **只校验带 `[F#]` 的 claim**：该句里 ≥1e6 的数字必须与所引 fact 相符（相对误差 0.5%） | 纯规则，0 成本 |
| 引用 | 句柄存在 + 在 evidence 里 + claim 级语义支撑率 ≥0.8 | 逐 claim 调用（见 §8 偏差） |
| 政策 | 过度断言（保证/必然/一定会…）；文档注入值是否被采纳 | 纯规则 |

失败后 `repair` 把**具体问题**喂回去，最多 2 轮；仍不过则带 `verification_failed=true` 返回。
评测不会因为一条答案不达标而挂住；报告里有"未过校验发布"一列单独统计。

**数字检查比计划窄，这是有意的。** 计划要求"量级 ≥1e6 的数字都要对上 facts/compute"。
拿 4 seed 的 142 个含大额数字的答案试跑，**44 个会被误报（31%）**——因为正文引用（`[K#]`）里的
数字是这个语料合法的数字来源，compute 结果也没有账本。严格照做会让 repair 永远在改无关内容。
所以只保留"引了 F# 就必须对得上"这条无歧义规则，纯文本数字交给引用语义判官。

**引用检查仍是逐 claim，不是计划里的一次批量调用。** 批量会改变已用 60 条盲标校准过的判定口径
（κ=0.634），W4 不再动判官；实测单题成本只增加约 3%，没有触发预算问题。

三个实测效果：

1. **引用支撑率 +3.5pt**（4 seed 均值 react 90.9% → graph 94.4%）；对抗题引用从 0.6 → 0.8。
2. **误拒不会被误修**：拒答在 `verify` 直接放行，不再进入两轮无意义的 repair（有回归测试）。
3. **注入与过度断言能被拦**：答案采纳文档里的 `999 million` 会判失败；写明"已忽略"不会误伤
   （`tests/test_graph.py` 里有正反两个用例）。

## 4. HITL：中断是真的，恢复也是真的

```
$ make graph-ask Q="苹果 FY2024 的总净营收是多少美元？" ARGS="--review"
待审 thread: 20261111-220141-adhoc-1b5964
草稿: 苹果 FY2024 … 391,035,000,000 美元 … [F1]
校验通过: True / []

$ make resume THREAD=20261111-220141-adhoc-1b5964 ACTION=approve
答案: … [F1]   cost=$0.00031
```

第二条命令是**新进程**。完整证据还包括一次真实的 `kill -9`：

```
$ python -u /tmp/hitl_hold.py        # 跑到 interrupt，打印 thread，然后 sleep
THREAD 20261111-230344-kill9-cbfcfb
$ kill -9 <pid>
$ make resume THREAD=20261111-230344-kill9-cbfcfb ACTION=approve
答案: 苹果 FY2024 … 3910.35 亿美元 [F1]
```

自动化回归在 `tests/test_graph.py::test_postgres_checkpoint_survives_new_agent`：
两个独立 `GraphAgent` 实例（各自连接）共享同一个 Postgres checkpoint，验证同一 thread 可恢复。

设计上做了两个刻意的隔离：

- `Agent.answer()` 永远是单次请求/响应，评测入口 `REVIEW_MODE=never`；人工入口走
  `start_review()` / `resume()`，不让评测卡在 interrupt 上。
- 中断 payload 只放草稿、校验报告、引用，不放任何连接或对象。

## 5. MCP：契约比功能更难

- `mcp_server/` 4 个工具，服务端**无状态**：只返回证据记录，不返回 `K#`/`F#`；句柄由客户端分配。
- 连接层只读：`PGOPTIONS=-c default_transaction_read_only=on`，即使服务端代码有 bug 也写不进仓。
- 契约测试 **12 条**：工具清单与 schema 一致性、四工具缺参拒绝矩阵、三种 not_found、空检索不算错、
  正常返回、无句柄证据、句柄重建、超时、只读静态检查、句柄重映射、接口逐字节等价。
- **待审列表不是靠记 thread_id**：`review_queue` 表在 `interrupt()` 前写入、`finalize` 时删除；
  Streamlit trace 页顶部列出待审运行，支持通过 / 拒绝 / 改写后通过（`agent/review_queue.py` + `web/trace_app.py`）。
- **stdio 的 cancel scope 必须在同一任务内进出**：第一版跨节点复用 Client，`aclose()` 直接抛
  `Attempted to exit cancel scope in a different task`，评测进程挂死。改成常驻 worker 任务后解决（ADR-007）。
- **等价测试抓到一个真 bug**：服务端的 `ToolExecutor` 没有传 `Retriever.from_settings`，
  于是 `RETRIEVAL_TIER=hybrid` 时 MCP 路径会静默退回 BM25。单看端到端分数发现不了——
  因为当时的 `.env` 是 bm25。修复后才算真正等价。

**工具描述实验**（20 条路由用例，只选工具不执行）：

| 描述版本 | 工具选对 | 工具 + 参数全对 |
|---|---|---|
| 现行描述 | 17/20 | 17/20 |
| 极简描述（"搜索财报"） | 15/20 | 15/20 |

10 个百分点的差距说明：工具描述是接口的一部分，不是注释。

## 6. 50 题与扩题

| 类别 | W3 | W4 |
|---|---|---|
| F1 事实 | 5 | **10**（+5 引用陷阱） |
| F2 多跳 | 15 | 15 |
| C1 计算 | 5 | 5 |
| C2 对比 | 10 | 10 |
| A1 对抗 | 5 | 5 |
| R1 不可答 | 5 | 5 |
| 合计 | 45 | **50** |

引用陷阱题的共同特征：数字在整份 10-K 里**只出现在 Item 8**，Item 7 只做定性讨论。
引错段落不会被数值判分抓住，但会被引用校验抓住——这就是它们存在的意义。

## 7. 三个发现（也是三次返工）

**① 换图后的第一版确实掉分：84.4% vs 97.8%。**
根因不是图，是**丢了一个能力**：手写循环在步数/预算耗尽时会做一次"无工具的 finalize 调用"，
把已有证据抢救成答案；图的第一版直接返回拒答。补上 `finalize` 的 salvage 调用后回到 93.3%。

**② 步数上限是 F2 的主要失败模式。**
8 步时仍有 3 道 F2 撞上限并拒答；把两个 Agent 的预算统一提到 9 步后，F2 从 85% 回到 90%，
总分打平。**任何"提升"都必须同时改两个 Agent 的预算，否则比的是预算不是架构。**

**③ 校验回环的价值在引用，不在总分。**
总分打平，但引用支撑率 +3.5pt、对抗题引用 +20pt。想靠 verify 把 95 刷到 98 是不现实的；
它的价值是"发出去的每句话更可信"。

## 8. 与计划的偏差

| 计划 | 实际 | 原因 |
|---|---|---|
| 验收"正确率 ≥65%" | 改为"不退化 + 三项架构证据" | 65% 在 W3 已超额（已回写主设计 §0.2） |
| `max_steps=7` | A/B 统一 **9** | 8 步仍有 3 道 F2 撞顶；两个 Agent 同步调整，保证公平 |
| 查询分解 | 未做 | W3 已证明检索饱和；优先保 HITL/MCP 的真实性 |
| CrewAI 对照 | 未做（只写 ADR 分析） | 同上 |
| 第三方 MCP 互操作 | 未做 | 演示价值 > 能力证明，留给 W5 |
| 子图 | 扁平图 | 单流程无复用收益（ADR-006） |
| MCP 超时 3s | **5s** | 检索含 embedding/rerank，3s 会误杀（ADR-007） |
| 数字检查覆盖"量级 ≥1e6 的数字" | **只查带 `[F#]` 的 claim** | 4 seed 142 个答案里 44 个（31%）会被宽规则误报，repair 会空转 |
| 引用检查一次批量调用 | **沿用 W3 逐 claim checker** | 批量会改掉已校准的判定口径（κ=0.634）；实测成本只 +3% |
| 预算超限也触发人工 review | 预算超限直接 finalize | 评测必须能收敛；review 只在 `always` 或校验修满失败时触发 |
| 政策检查含"拒答与证据是否一致" | **未做成规则** | 有证据时拒答也可能正确（问题超出证据范围）；硬规则会误伤，交给判官与人工 |
| checkpoint 表单独 schema | 默认 public schema（表名 `checkpoints` / `checkpoint_*`） | 不影响业务表；独立 schema 需要额外连接参数，收益低 |
| trace 记 `checkpoint_ids` | 只记 `thread_id` + `nodes_visited` + `interrupts` | checkpoint id 可由 `graph.get_state(thread)` 再查，trace 不重复存 |
| 演示 GIF（中断 → 恢复） | 未录 | 用本报告第 4 节的终端记录替代；S6 再做 5 分钟视频 |
| `make compare-agents MODE=...` | `make compare-agents A=... B=...` | 自动找"最近两份报告"容易比错文件；显式传路径更安全 |

## 9. 限制

- **n 小。** 每个 seed 45 道计分题；4 seed 合并 180 对，CI 仍有 ±3.5pt。小于 2 题的差异不要解释。
- **判官不是神。** LLM 判官 κ=0.795；F2 的"部分正确"仍有争议。
- **MCP 端到端 A/B 只有 2 seed**（deterministic 判官，F2 不计分），CI `[−6.7,+3.3]`；工具层另有
  逐字节等价测试，所以"进程边界不改变工具行为"不依赖统计。
- **p95 受 provider 抖动主导**，13s 与 14s 的差别可能来自网络，不是代码。
- **c2-004 仍是已知歧义题**：WMT 的 `net sales` 与 `total revenue` 都合理，两个 Agent 都偶尔选错。

## 10. W5 输入

1. 镜像化：app + mcp_server 两个进程，`compose` 一条命令起全栈。
2. CI 门禁：引用支撑率下降 >2pt、误拒率上升 >5pt 阻断合并。
3. 部署：先用国内云 + IP，绑域名再说备案。
4. 可观测性：Langfuse 接 trace，补成本看板（含 embedding/rerank 单价）。
5. p95：继续观察 `hedged/hedge_won`，不做无证据的"优化"。
6. 查询分解：只有在 W5 有余量时才做，且必须用配对实验证明收益。


## 11. 计划完成度审计（2026-11-12）

按 `m3-w4-plan.md` 逐条核对。**"完成"以代码/测试/报告可复现为准，不以本报告的自述为准。**

| 计划条目 | 状态 | 证据 / 说明 |
|---|---|---|
| A 图骨架（state/nodes/build/checkpointer） | ✅ | `agent/graph/`；`tests/test_graph.py` 11 条 |
| B 校验回环：数字 | ✅ | `_numeric_problems`，只查 F# claim；4 条单测 |
| B 校验回环：引用 | ✅ | 句柄存在 + evidence + claim 级语义；W3 checker |
| B 校验回环：政策 | ✅ 2/3 | 过度断言 + 注入值采纳（正反用例）；"拒答一致性"未做，见 §8 |
| B 注入错误答案被拦并修复 | ✅ | 坏句柄 → repair → 通过；修满 → `verification_failed` |
| C HITL：interrupt / resume | ✅ | `make graph-ask --review` → `make resume`；真实 `kill -9` 后恢复 |
| C HITL：CLI | ✅ | `agent/graph_cli.py` |
| C HITL：Streamlit 待审列表 | ✅ | `review_queue` + trace 页"待审"区，支持通过/拒绝/改写 |
| D MCP：4 工具独立进程 | ✅ | `mcp_server/`，stdio，只读 `PGOPTIONS`，5s 超时 |
| D MCP：契约 4×4 | ✅ | 12 条测试：缺参矩阵、not_found、空结果、超时、schema、只读 |
| D MCP：句柄客户端分配 | ✅ | 服务端只回证据记录；客户端重建 K/F 句柄 |
| D 工具选择 before/after | ✅ | 17/20 vs 15/20（现行 vs 极简描述） |
| E 50 题 + 5 引用陷阱 + tags | ✅ | `gold_v2.jsonl`；`q-check` 50 题全过 |
| F A/B-1 换图不退化 | ✅ | 4 seed；95.0% vs 95.0%，CI `[−3.3,+3.9]` |
| F A/B-2 换 MCP 边界 | ✅ | 2 seed；98.3% vs 96.7%，CI `[−6.7,+3.3]`；接口逐字节等价 |
| G ADR-006 / 007 / 002 增补 | ✅ | `docs/adr/` |
| 上下文裁剪迁入图 | ✅ | `_trim_messages`（计划 §6.5 要求；首版遗漏） |
| trace graph 块 | ✅ | `meta.graph`：thread_id / nodes_visited / interrupts / repairs |
| `verification_failed` 单独统计 | ✅ | 报告总览"未过校验发布"一行 |
| A/B 成本/延迟增量 ≤50% | ✅ | 成本 +3%，p95 +0.6s |
| CI 离线全绿 | ✅ | [run 43135722375](https://github.com/evinyang-zw/fintrace/actions/runs/43135722375) |
| `make compare-agents` 可复现 | ✅ | `make compare-agents A=... B=...`（别名） |
| README 状态表 | ✅ | 已更新到 W4 |
| 简历 bullet | ✅ | `docs/resume-bullets.md` |
| 演示 GIF（中断 → 恢复） | ⏳ 延期 | 用户同意留到 W6；本报告第 4 节有终端记录 |
| 查询分解 / CrewAI / 第三方 MCP | ⏳ stretch | 计划允许；未做，不冒充完成 |

**审计结论**：计划的必做项全部落地。首版遗漏了三处（数字/政策校验、上下文裁剪、
Streamlit 待审列表），已在本轮补齐并加了回归测试；两处按证据收窄（数字规则、引用批量），
已在 §8 记录原因与测量数据。
