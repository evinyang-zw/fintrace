# ADR-007：工具层的 MCP 边界——无状态服务端，客户端发句柄

- 状态：已接受（W4）
- 日期：2026-11-11
- 关联：主设计 §3.1 ③、ADR-006

## 背景

W2/W3 的四个工具（`search_filings` / `get_section` / `xbrl_fact` / `compute`）是进程内函数，
直接改一个 `EvidenceStore`。W4 要把它们变成独立进程，同时保持 Agent 侧行为不变。

## 决策

1. **服务端无状态**：每次调用新建 `EvidenceStore`，只返回证据记录，**不返回 `K1`/`F2` 句柄**。
2. **句柄由客户端分配**：句柄是"本次运行内的编号"，属于 Agent 状态，不属于工具服务。
3. **stdio 传输**：本地与 CI 都能零配置起；streamable HTTP 留到 W5 部署。
4. **连接层只读**：客户端拉起服务时注入 `PGOPTIONS=-c default_transaction_read_only=on`，
   即使服务端代码有 bug 也写不进仓。
5. **单连接 worker 任务**：MCP 的 stdio 客户端持有 anyio cancel scope，必须在同一任务内进出；
   图里每个节点是不同任务，因此由一个常驻 worker 持有连接、串行处理调用。
6. **超时 5s**（覆盖检索的 embedding/rerank；冷启动不计入工具调用）。

## 理由

**为什么服务端不能有句柄**：句柄顺序依赖本次运行的证据累积。如果服务端维护计数器，
它就必须保存会话状态，无法缓存、无法并发、也无法单独测试。

**为什么要 worker 任务**：第一版把 `Client` 放在 `AsyncExitStack` 里跨节点复用，
结果在 `aclose()` 时抛 `Attempted to exit cancel scope in a different task`，
并且让评测进程在退出时挂死。worker 任务是这个约束下最简单可靠的解法。

**为什么现在不做第三方 MCP 互操作**：W4 的 20 小时优先保证自建工具的真实性；
互操作是演示价值，不是能力证明，放到 W5 有余量再说。

## 代价

- 每次工具调用多一次进程间往返（实测 <50ms，相对 LLM 延迟可忽略）。
- 服务端需要数据库与 embedding/rerank 的凭证，部署时要单独注入。
- 工具 schema 与 `TOOL_SPECS` 是两份定义，需要契约测试防止漂移。

## 被否决的方案

| 方案 | 为什么不用 |
|---|---|
| 保持进程内函数 | 无法证明工具层可独立部署、独立限权、独立测试 |
| 服务端分配句柄 | 需要会话状态，破坏可缓存/可并发 |
| 每次调用新建 Client | 每次拉起一个 Python 子进程，50 题评测会慢到不可接受 |
| 现在就上 streamable HTTP | 部署在 W5，提前做会挤掉 HITL 与校验回环的时间 |
