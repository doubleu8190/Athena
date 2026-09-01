# Athena LangGraph + SSE + Web 重构详细架构与实施方案

## 1. 文档定位

本文定义 Athena 从 Electron + WebSocket + 自定义 Agent Harness 迁移到 Web + SSE + LangGraph 的目标架构和实施顺序。

以下文档仅作为技术参考，不是实施指令；冲突时以本文为准：

- `基于 SQLite Event Store + LangGraph Checkpoint 的 Agent SSE Endpoint 事件推送机制设计.md`
- `LangGraph + SQLite + SSE 流式事件推送技术方案.md`

本文基于当前 Athena 代码和已经确认的产品决策编写。本阶段不修改业务代码或数据库。现有数据允许全部清除，因此不设计旧数据迁移和旧运行中会话兼容。

## 2. 已确认约束

1. 第一版运行在一个 OS 进程中，但 Gateway 与 Agent Runtime 必须具备可独立进程部署的代码和协议边界。
2. 一个会话对应一个 LangGraph Thread，即 `session_id == thread_id`。
3. 一次用户请求产生一个 `run_id`；每次客户端操作拥有独立 `command_id`。
4. 前端改为浏览器 Web 应用，用于本机或局域网，单用户但支持多个客户端同时观察。
5. 用户可通过界面暂停和恢复任务；暂停期间允许提交新的 `message.submit`，新消息为同一 Session 创建新的 `run_id` 并进入待处理队列。取消不再作为不可恢复的 Stop 语义单独定义。
6. 流式输出采用 Realtime Delta + SQLite Stream Snapshot + Durable Event 三层模型。
7. 除 Evaluation 外保留现有核心功能；Evaluation 前后端及运行时完整移除。
8. 登录使用 `.env` 中的单个用户名和密码，成功后签发 HttpOnly Cookie。

## 3. 目标与非目标

目标：

- Runtime 不依赖 FastAPI、SSE、WebSocket、浏览器或 Gateway DTO。
- Gateway 不依赖 LangGraph Graph、Node、Tool executor 或 Runtime State。
- 双方只交换可序列化、带 schema version 的 Command 与 Application Event。
- 替换 SSE 或把 Runtime 拆为独立进程时，不修改 Runtime 业务逻辑。
- 支持命令幂等、SSE 重放、多客户端、取消和持久审批。
- 保留会话、工具、子 Agent、记忆、文件、MCP、Provider 和设置能力。

非目标：

- 第一版不支持多用户、跨主机和多 Uvicorn Worker。
- 不引入 Redis、Kafka 或 NATS。
- 不保证任意外部工具副作用的 exactly-once。
- 不兼容旧数据库、旧 WebSocket 客户端或 Electron 安装包。
- 不实现从 LLM 中间 Token 继续生成。

## 4. 单进程下的进程级边界

这里的“等同两个进程”指依赖、协议和部署可拆分性等同：

- Gateway、Runtime、Contracts、Infrastructure 分包。
- 只有组合根可以同时引用 Gateway、Runtime 和具体 Adapter。
- 所有跨边界对象都可 JSON 序列化，禁止传 callback、Request 或 Graph 对象。
- 进程内通信只是 Port 的一种 Adapter，可替换为 IPC Adapter。
- 用架构测试禁止 Gateway 与 Runtime 相互 import 实现。

一个进程无法获得真正的故障、CPU、内存或安全隔离；Runtime 的未捕获进程级故障仍可能使 Gateway 一起退出。

## 5. 总体架构

```text
Browser
  |-- HTTP Commands
  |-- SSE Events
  `-- Static Assets
           |
           v
+-------------------------------------------------------+
| Single OS Process                                     |
|                                                       |
| Gateway: Web / Auth / REST / SSE / Projection         |
|                 |                                     |
| Contracts: Command / EventEnvelope / Ports    |
|                 |                                     |
| Infrastructure: Command Store / Event Store /         |
|                 Snapshot Store / InProcess Transport  |
|                 |                                     |
| Runtime: Consumer / LangGraph / Tools / Recovery /    |
|          EventPublisher / StreamCoalescer              |
+-------------------------------------------------------+
             |                       |
             v                       v
             athena.sqlite
```

所有数据统一存放在一个 `athena.sqlite` 中：业务表、Command、Event、Snapshot、Approval、Tool Ledger 以及 LangGraph Checkpoint 共用同一数据库连接和 WAL。LangGraph Checkpointer 只能访问其专用表，禁止业务代码直接依赖 Checkpoint 内部表结构；业务表和 Checkpoint 之间需要原子性的状态转换使用同一数据库事务完成。

## 6. 模块与依赖

建议结构：

```text
athena/
├── contracts/              # commands, events, enums, ports
├── agent_runtime/
│   ├── command_consumer.py
│   ├── recovery.py
│   ├── graph/              # state, nodes, routing, subgraphs
│   ├── events/             # publisher, coalescer, classification
│   └── tools/
├── gateway/
│   ├── auth/
│   ├── routes/
│   ├── sse/                # endpoint, feed, projection
│   └── web.py
├── infrastructure/
│   ├── sqlite/             # command/event/snapshot repositories
│   └── transport/in_process.py
└── main.py                 # 唯一组合根
```

允许的依赖：

```text
gateway        -> contracts
agent_runtime  -> contracts
infrastructure -> contracts
main           -> gateway + agent_runtime + infrastructure
```

禁止的依赖：

```text
agent_runtime -X-> gateway
gateway       -X-> agent_runtime
contracts     -X-> 其他三层
graph nodes   -X-> FastAPI/SSE/WebSocket
SSE endpoint  -X-> LangGraph State/Checkpoint internals
```

## 7. 标识与幂等

| 标识 | 含义 | 生命周期 |
|---|---|---|
| `session_id` | 会话及 LangGraph `thread_id` | 整个会话 |
| `run_id` | 一轮 Agent 执行，由 Gateway 生成 | 消息提交至完成、取消或失败 |
| `command_id` | 客户端意图幂等键 | 一条命令 |
| `session_seq` | Durable Event 在 Session 内的连续游标 | 一条持久事件 |
| `stream_id` | 一段 Assistant 流 | 一条流式消息 |
| `tool_execution_id` | 工具执行账本 ID | 一次工具尝试 |
| `approval_id` | 可竞争决策的审批项 | 一次 Interrupt |

`command_id` 必须保留并在整个系统内全局唯一。HTTP 响应丢失、重试、重复点击、多标签页提交和服务重启后的 at-least-once 重投都可能重复执行用户意图。数据库对其做唯一约束，重复提交必须返回第一次存储的原命令及其 `run_id`。

它不能用消息文本替代，因为用户可能有意连续发送相同文本；也不能用服务端 `run_id` 替代，因为客户端首次提交 `message.submit` 时尚未拥有 `run_id`。Gateway 必须在同一 `athena.sqlite` 事务中生成 `run_id`、插入 `agent_runs` 并存储 Command Envelope。`run_id` 不属于客户端请求 DTO，只属于存储后的 Command Envelope 及 Gateway 响应；并发重投命中唯一约束时不得重新生成 `run_id`。

## 8. Command 协议

```json
{
  "schema_version": 1,
  "command_id": "cmd_...",
  "command_type": "message.submit",
  "session_id": "session_...",
  "run_id": "run_...",
  "issued_at": "2026-08-28T10:00:00Z",
  "payload": {}
}
```

规则：

- Payload 写入前和消费前均用 Pydantic schema 校验。
- 未知 major version 拒绝消费。
- Runtime Command 包括 `message.submit`、`run.cancel`、`approval.resolve`、`approval.cancel`、`memory.create`、`file.retry` 和 `file.cancel`。
- Session 创建、查询和重命名不驱动 Graph，可由 Gateway Application Service 处理。
- 改变活动 Runtime 状态的操作必须经过 Command Bus。

Command Envelope 不包含通用的 `expected_version` 字段。当前产品是单用户模式，资源并发控制由各自的状态机和数据库条件更新完成；引入一个没有明确语义归属的通用版本字段，只会让每种 Command 额外定义版本对应的资源、读取时机和冲突规则。未来如果出现真正的基于旧快照覆盖资源的命令，应为该命令定义明确的资源版本字段或专用条件，而不是重新添加到所有 Command。

Command 状态机：

```text
PENDING -> CLAIMED -> SUCCEEDED
                  `-> FAILED / REJECTED
```

单机单消费者按序领取命令。HTTP 接受命令后返回 `202 Accepted`。相同全局唯一 `command_id` 返回第一次存储的原命令、`run_id` 和当前状态；相同 ID 但 Command 类型、Session 或 Payload 不同返回 `409 command_id_conflict`。`message.submit` 的 `run_id` 由 Gateway 生成并仅写入存储后的 Envelope。

Command 提交事务成功后，Gateway 通过进程内通知唤醒 Runtime Consumer；通知只负责唤醒，不携带也不替代 Command 数据。Consumer 启动时先从 SQLite 扫描待处理命令，空闲时订阅通知，收到通知后从 SQLite 持续领取命令，避免在正常运行期间持续轮询，同时保证通知发生在订阅前也不会丢失已持久化命令。

`message.submit` 由 Gateway 负责接收和判定是否获得 Run 创建权。Gateway 可先快速检查对应 Session 是否已存在非暂停的非终态 Run；如无非暂停的非终态 Run，再以非阻塞方式尝试获取 Session 行锁。只有获得锁的请求才获得 Run 创建权并能够开始创建；检查发现非暂停的非终态 Run 或尝试获取锁失败时，请求不入队、不生成新 `run_id`，直接返回 HTTP `409 session_busy`。如果仅存在 paused Run，则创建新的 `run_id` 并正常入队。获得锁后，Gateway 在该锁保护的事务中生成 `run_id`、创建 Run 和存储 Command，提交事务后释放锁。锁竞争不等待，以免请求在竞争时排队。对于同一 `command_id` 的重试，应先应用幂等规则；如果首次提交已被存储，不得因当前 Session 后来变为 busy 而改变原始响应。

### 8.1 Session Run 创建权与串行执行

Gateway 是所有前端 HTTP 消息的接入点。对 `message.submit`，Session 行锁就是 Run 创建权：

1. Gateway 先检查 Session 是否已存在非暂停的非终态 Run；已存在时立即返回 `409 session_busy`，仅存在 paused Run 时允许创建新的 Run。
2. 检查通过后，Gateway 以非阻塞方式尝试获取 Session 行锁；失败立即返回 `409 session_busy`，不等待。
3. 只有获得锁的请求才能在锁保护的事务中生成 `run_id`、创建 Run 和存储 Command，事务提交后释放 Session 锁。
4. Runtime Consumer 在单机单消费者模型下按命令顺序处理，并使用 LangGraph Checkpoint 保存可恢复的图状态。
5. Session 行锁只保护创建权事务，不跨运行期持有；paused Run 不占用新消息的 Run 创建槽。

SQLite 不提供真正的行级锁；本文的“Session 行锁”是逻辑语义。Run 创建事务使用 `timeout=0`/非阻塞 `BEGIN IMMEDIATE`，锁竞争立即返回 `409 session_busy`，不能等待其他请求释放锁后再创建。普通读写连接可使用 5 秒 `busy_timeout`。获得锁后才执行“创建 Run -> 存储 Command -> 提交事务”。

同一 Session 的 `message.submit` 在已存在非暂停的非终态 Run 时会被 Gateway 以 `409 session_busy` 拒绝；仅存在 paused Run 时，新消息创建独立 Run 并正常执行。`run.cancel`、`approval.resolve` 等控制命令仍可进入协调器，以便改变目标 Run 状态。一个 Thread 可以拥有多个历史 Run；同一时刻最多一个非暂停的非终态 Run，paused Run 可以与新的 queued Run 共存。Run 按命令创建顺序串行执行并复用该 Thread 的 Checkpoint。

## 9. Application Event 协议

```json
{
  "schema_version": 1,
  "session_seq": 123,
  "event_type": "tool.completed",
  "durability": "durable",
  "session_id": "session_...",
  "run_id": "run_...",
  "stream_id": null,
  "occurred_at": "2026-08-28T10:00:01Z",
  "payload": {}
}
```

规则：

- 前端只依赖 Application Event，不依赖 LangGraph 原始 event。
- Event name 使用点分层级并保持向后兼容。
- `session_seq` 只分配给 Durable Event，并在每个 `session_id` 内从 1 严格连续递增；不同 Session 可出现相同 `session_seq`。
- 每种事件是否需要长期保存，由 Runtime 的统一规则决定；Graph Node 只负责报告发生了什么，不能自行把重要事件标记为“只实时发送、不保存”。

事件分层：

| 层级 | 存储与传输 | 示例 |
|---|---|---|
| Realtime | 内存 Transport，可丢 | `message.delta`, `reasoning.delta` |
| Snapshot | 定期覆盖写 SQLite | `stream.snapshot` |
| Durable | 追加写 Event Store | Run、Tool、Approval、Message、File 生命周期 |

核心事件：

```text
run.queued / started / cancel_requested / cancelled / completed / failed
llm.started / completed / failed
message.stream_started / delta / completed
tool.started / progress / completed / failed / skipped
approval.required / resolved / expired
subagent.started / progress / completed / failed
memory.search_started / search_completed / saved
context.compression_started / completed
file.task_created / progress / completed / failed
file.attachment_updated / run.waiting_for_files
```

Durable Event Writer 可按 100 条或 50ms 批量提交，但 `publish()` 返回成功必须表示事件已 commit。写入时在同一事务中锁定或条件更新 Session 的 `next_event_id`，再插入事件；事务回滚不得消耗编号，以保证 Session 内无跳号。关键业务状态、Command 状态和对应 Durable Event 在同一事务中更新；LangGraph Checkpoint 与业务 Event 采用最终一致语义，由 Reconciler 根据稳定的 `transition_id` 幂等补写缺失状态或事件。Event 永久保存，不设置 TTL 或自动清理策略；数据库备份和归档必须保留完整 Event Store。

## 10. 流式输出与 Snapshot

Runtime 不发送单 Token Event。`StreamCoalescer` 在以下任一条件满足时发布 `message.delta`：

```text
50ms OR 512 bytes
```

Delta 必须包含：

```json
{
  "stream_id": "stream_...",
  "base_version": 12,
  "start_offset": 2048,
  "end_offset": 2084,
  "delta": "合并后的文本"
}
```

Snapshot 在以下任一条件满足时覆盖写入 SQLite：

```text
500ms OR 50 tokens OR 4KB pending content
```

输出完成、取消、失败和 Runtime shutdown 前必须强制 flush：

```json
{
  "session_id": "session_...",
  "run_id": "run_...",
  "stream_id": "stream_...",
  "version": 13,
  "content": "截至当前的完整内容",
  "content_length": 2084,
  "status": "streaming",
  "updated_at": "2026-08-28T10:00:02Z"
}
```

前端仅在 `start_offset == local_content_length` 时追加 delta；重复、落后或存在 gap 时以更高版本 Snapshot 重新对齐。最终 `message.completed` Durable Event 携带 `message_id`、`stream_id` 和最终 Snapshot version。

## 11. 进程内 Realtime Transport

Contracts 定义 `RealtimeTransport.publish/subscribe` 端口。第一版 Adapter 使用按 subscriber 隔离的有界 `asyncio.Queue`，采用阻塞背压：

- Queue 满时 `publish()` 必须等待空间，不丢弃 Realtime Delta，也不继续向该 Session 投递后续事件。
- 背压按 Session 隔离；一个 Session 的慢客户端可以阻塞该 Session 的流式生产，但不能阻塞其他 Session 的 Runtime 工作。
- Gateway 必须及时检测 SSE 断开并释放 Subscription，使生产者从阻塞中恢复。
- Queue 等待必须可被 Runtime shutdown、Run cancel 和 Subscription cancel 唤醒，不能形成无法取消的永久等待。
- Durable Event 不以该 Queue 作为唯一存储。
- Gateway 连接关闭只释放 Subscription，不取消 Run。

未来拆进程时替换为 Unix Socket、Redis 或 NATS Adapter；Runtime、Gateway 和协议保持不变。

## 12. SSE Gateway

端点：

```http
GET /api/sessions/{session_id}/events
Accept: text/event-stream
Last-Event-ID: 123
```

Durable Event 使用 Session 内的 `session_seq` 作为 `id:`；Realtime Event 不设置 Durable ID，避免浏览器把不可重放的 delta 当作可靠游标。`Last-Event-ID` 只在 URL 指定的 Session 内解释。

原生 `EventSource` 自动重连会发送 `Last-Event-ID`，但页面刷新后 JavaScript 不能自行设置该 Header。因此端点同时支持 `?after=<session_seq>`：服务端优先使用合法的 `Last-Event-ID`，否则使用 `after`。前端在本地持久化每个 Session 最后处理的 Durable `session_seq`，刷新后通过 `after` 恢复。该游标只用于定位，不承载认证或权限信息。

### 12.1 Replay 与 Live 无缝切换

为避免查询历史结束后、订阅实时前产生空窗，Gateway 必须：

1. 先创建 Session Feed Subscription。
2. 读取 Event Store 当前 Durable high-water mark。
3. 从 `Last-Event-ID` 重放到 high-water mark。
4. 发送当前 active stream snapshots。
5. 开始消费 Subscription 中的 live notification。
6. Durable notification 按 Session 内 `session_seq` 去重；发现不连续时回 Event Store 补查。
7. Realtime delta 按 `stream_id + offset` 合并；不连续时 snapshot resync。

Event Store 是 Durable replay 唯一真相源；进程内通知只用于降低延迟。

### 12.2 连接规则

- 每 15 秒发送 SSE comment heartbeat。
- 客户端断开不影响 Runtime。
- Run terminal 后不强制关闭，因为同一 Session 后续 Run 复用连接。
- 同一 Session 支持多个 EventSource，各自维护游标和 UI projection。
- 前端 Durable Event 按 `session_seq` 去重；Message、Tool 和 Approval 分别按稳定 ID upsert；Snapshot 仅接受更高 version。

## 13. LangGraph Runtime

### 13.1 Thread 与 State

```text
LangGraph thread_id = session_id
```

每次调用携带 `run_id` 和 `command_id` metadata。同一 Thread 保存多轮对话；业务代码不直接读写 Checkpoint 表。

建议 State：

```text
messages                 使用 LangGraph message reducer
active_run_id            当前 Run
request                  当前请求和附件引用
memory_context           本轮召回结果
budget                   轮次、重试和 Token 预算
pending_tool_calls       待执行工具
tool_results             按 execution_id 合并
pending_approval         Interrupt 描述
  cancel_requested         协作式取消标志
active_stream_id         当前输出流
error                    结构化错误
```

大文件内容、Event 历史、完整检索索引和 UI 状态不进入 Checkpoint。

### 13.2 Graph 流程

```text
START
  -> prepare_request
  -> wait_for_files? ------ interrupt/wakeup
  -> retrieve_memory
  -> prepare_context
  -> call_llm
  -> route_response
       |-> finalize_response -> post_process -> END
       `-> prepare_tools
             |-> approval_gate? -> interrupt
             `-> execute_tools (parallel-safe)
                    -> append_results
                    -> cancel_gate?
                    -> call_llm
```

现有预算、Provider fallback/retry、错误分类和上下文压缩应迁移为节点或 Runtime Service，不能因接入 LangGraph 而丢失。暂停时保存可恢复 Checkpoint；恢复继续当前 Run。暂停期间提交的新消息创建新的 Run，按顺序进入队列，不能并发改写同一 Thread。

LangGraph callback/event 必须经过 Adapter 转为 Application Event，不向前端暴露原始框架 payload。

## 14. 工具、子 Agent 与副作用

每次工具执行先建立 Tool Execution Ledger：

```text
tool_execution_id / session_id / run_id / tool_call_id
tool_name / arguments_json / arguments_fingerprint / redaction_policy_version
status / attempt / retry_of_execution_id
side_effect_class / result / error / timestamps
```

`arguments_json` 和 `arguments_fingerprint` 必须同时保存，职责不同：

- `arguments_json`：保存经过脱敏和大小限制后的审计参数，用于审计、错误排查和前端展示；它不承诺可用于重建包含秘密值或大内容的工具调用。
- `arguments_fingerprint`：使用服务端密钥，对未脱敏的原始规范化参数计算 keyed HMAC，用于判断重试参数是否一致、辅助去重和检测记录完整性。原始参数和 HMAC 密钥均不写入 Ledger。
- `redaction_policy_version`：标识生成该条 `arguments_json` 时所采用的脱敏规则版本，例如 `tool-args-v1`。规则版本定义哪些字段按名称或路径删除、哪些值掩码、正文和集合如何截断，以及二进制或大内容如何替换为摘要。它不是秘密，也不参与恢复执行；它用于解释历史审计记录，并允许脱敏规则升级后仍能判断旧记录为何呈现为当前形式。

原始参数不能落库。API Key、密码、Cookie 等敏感字段必须从审计 JSON 中脱敏或删除；大段文件内容、二进制数据和超大 Shell 输入应保存摘要、外部 Blob ID 或截断后的安全版本。HMAC 应基于脱敏前的原始规范化参数计算，规范化至少包括递归排序对象字段、统一编码和去除不影响语义的格式差异。HMAC 密钥必须来自独立配置，不复用认证 Session Secret；密钥轮换时另存非敏感的 fingerprint key version，以便区分不可直接比较的历史指纹。

因此，两个语义相同但字段顺序不同的参数应得到相同 fingerprint；在同一 fingerprint key version 下，两个指纹不同则视为不同的工具请求。不同秘密值即使在 `arguments_json` 中都显示为相同掩码，指纹仍然不同。

每次执行尝试都生成一个全局唯一的新 `tool_execution_id`。重试不会复用旧 ID，而是递增 `attempt` 并通过 `retry_of_execution_id` 指向上一次尝试；同一 `tool_call_id` 下的尝试链用于恢复判断和审计。

恢复规则：

- `SUCCEEDED`：复用结果，不重复执行。
- `RUNNING` 且可安全重试：创建具有新 `tool_execution_id` 的下一 attempt。
- `RUNNING` 且可能已有不可逆副作用：进入人工核对 Interrupt。
- `FAILED`：按 retry budget 决定重试或交回 LLM。

这不能为任意外部系统提供 exactly-once，但能防止已知成功的工具被无条件重复调用。

并行工具使用可合并 reducer 或按 `tool_execution_id` 独立持久化，禁止并发分支覆盖同一个普通 State 字段。

子 Agent 功能保留，优先实现为 LangGraph subgraph。每个子 Agent 有独立 `sub_run_id`；父 Run 取消时传播协作式 cancel request，并在安全点汇合。

## 15. 审批

当前基于进程内 Future 的审批实现必须替换为 LangGraph Interrupt + 持久化 Approval Record：

1. Approval gate 创建 `approval_id` 和不可操作的 PREPARING record。
2. 调用 `interrupt()` 并确认包含该 `approval_id` 的 Checkpoint 已保存。
3. 在 `athena.sqlite` 的同一事务中将 Approval 转为 PENDING、将 Run 转为 `WAITING_APPROVAL` 并 commit `approval.required` Durable Event。
4. 多个已登录客户端都可显示审批。
5. `approval.resolve` 作为带 `command_id` 的 Command 提交。
6. Runtime 使用 `WHERE approval_id = ? AND status = 'PENDING'` 的条件更新，将 `PENDING` 一次性转换为 `APPROVED/DENIED`，只允许一个客户端成功。
7. Runtime 用 `Command(resume=...)` 恢复同一 Thread；审批决定不是取消，仍可使 `WAITING_APPROVAL` Run 回到 `RUNNING`。
8. 其他竞争决定在 Command 处理结果中标记 `approval_already_resolved`。由于 resolve 本身是异步 Command，HTTP 接受请求仍返回 `202 Accepted`；只有在 Gateway 选择同步执行条件更新的专用实现中，才将该业务错误映射为 HTTP `409`。两种语义不得混用。

“异步 Command 与 HTTP 409”的边界如下：客户端提交 `approval.resolve` 时，Gateway 只负责校验、幂等落库并返回 `202`，此时尚不知道另一个客户端是否已经抢先处理。Runtime 随后按 Session 串行协调器执行条件更新；若发现 Approval 已不是 PENDING，则该 Command 进入 `REJECTED`，错误码为 `approval_already_resolved`，并发布对应 Durable Event，客户端通过 `GET /api/commands/{command_id}` 或 SSE 获得结果。若产品要求点击后立即得到 `409`，就必须把“条件更新 Approval”提升为 Gateway 同步事务，再将成功的 resume Command 入队；这会形成另一种专用 API 时序，不是当前统一的异步 Command 语义。

审批超时由 Runtime scheduler 产生显式 expire 操作，不依赖 SSE 连接存活。

Runtime 按 `session_id` 保证同一 Thread 的运行状态不被并发更新。`message.submit` 的 Run 创建权只在 Gateway 的 Session 锁事务中判定；`run.cancel`、`approval.resolve`、`approval.cancel` 和 `approval.expire` 则由 Runtime 根据当前持久化状态和条件更新执行。不同 Thread 可以并行，不要依赖一个按 Session 维护的内存 FIFO 队列来恢复顺序。

## 16. 取消与崩溃恢复

Run 状态机（同一 Thread 下 Run 串行，任一时刻最多一个执行中的 Run；暂停状态允许接收新的消息 Command）：

```text
QUEUED -> RUNNING -> COMPLETED / FAILED / CANCELLED
                  -> CANCEL_REQUESTED -> CANCELLED
                  -> WAITING_APPROVAL -> RUNNING / CANCEL_REQUESTED
                  -> WAITING_FILES -> RUNNING / CANCEL_REQUESTED

暂停与恢复由用户命令控制；暂停时 Graph 停在可恢复 Checkpoint，新的 `message.submit` 进入待处理队列，不创建并发执行分支。`COMPLETED`、`FAILED`、`CANCELLED` 均为终态，终态 Run 释放 Session 的运行占用。新 Run 复用同一 `thread_id=session_id`，并按创建顺序读取前一 Run 已提交的 Checkpoint。
```

安全点语义：

- Node 之间：在 cancel gate 保存 Checkpoint 后取消。
- LLM streaming：取消当前 Provider stream，将未完成 Snapshot 标记 `aborted` 后进入 `CANCELLED`。
- 可协作取消 Tool：发送 cancellation token，确认后取消。
- 不可安全取消 Tool：等待结束、写入 Ledger 后再取消，不自动重放。
- 等待审批或文件时：取消等待并将对应 Approval/File task 标记为 cancelled/closed。

Cancel Command 由 Runtime Consumer 处理：先持久化 `cancel_requested`，再通知 Runtime 内部的 Cancellation Registry。Cancellation Registry 属于 Runtime 内部实现，不跨 Gateway/Runtime 边界；Graph 的 cancel gate 仍以持久化 Run 状态为准，避免只依赖内存信号。

`run.cancel` 返回 202 只表示请求被接受；`run.cancelled` Durable Event 才代表取消完成。

### 16.1 Run 状态转换矩阵与错误码

`message.submit` 只在 Session 无非终态 Run 时被接受；其他控制命令由串行协调器根据以下矩阵处理。“拒绝”表示 Command 可入队但最终以 `REJECTED` 结束；`session_busy` 是 `message.submit` 在 Gateway 接受前的同步 HTTP `409`，不创建 Command。

| 当前 Run 状态 | `message.submit` | `run.cancel` | `approval.resolve/cancel` | `approval.expire` |
|---|---|---|---|---|
| `QUEUED` | `409 session_busy` | 允许，转 `CANCEL_REQUESTED` | 拒绝 `approval_not_pending` | 拒绝 `approval_not_pending` |
| `RUNNING` | `409 session_busy` | 允许，转 `CANCEL_REQUESTED` | 无当前 Approval 时拒绝 `approval_not_pending` | 拒绝 `approval_not_pending` |
| `CANCEL_REQUESTED` | `409 session_busy` | 幂等接受，返回原结果 | 拒绝 `run_cancel_in_progress` | 拒绝 `run_cancel_in_progress` |
| `WAITING_APPROVAL` | `409 session_busy` | 允许，转 `CANCEL_REQUESTED` | PENDING 时仅允许一次条件更新，其余 `approval_already_resolved` | 允许，转 `EXPIRED` 并恢复 Graph |
| `WAITING_FILES` | `409 session_busy` | 允许，转 `CANCEL_REQUESTED` | 拒绝 `approval_not_pending` | 拒绝 `approval_not_pending` |
| `COMPLETED` | 接受并创建新 `run_id` | 拒绝 `run_terminal` | 拒绝 `approval_not_pending` | 拒绝 `approval_not_pending` |
| `FAILED` | 接受并创建新 `run_id` | 拒绝 `run_terminal` | 拒绝 `approval_not_pending` | 拒绝 `approval_not_pending` |
| `CANCELLED` | 接受并创建新 `run_id` | 拒绝 `run_terminal` | 拒绝 `approval_not_pending` | 拒绝 `approval_not_pending` |

`run.cancel` 的 HTTP 接受响应为 `202`；取消完成由 `run.cancelled` Durable Event 表示。暂停/恢复使用独立的 `run.pause` 与 `run.resume` Command，恢复同一 Thread 的 Checkpoint；暂停期间提交的 `message.submit` 创建新的 Run 并按序执行。上表中的业务拒绝错误由 Command 结果和 Durable Event 表示。同一 `command_id` 重试始终返回原命令结果。

Run 状态和 LangGraph Checkpoint 是运行位点的权威来源；业务表只存储用于会话查询和 UI 展示的投影，不另外定义一个未在本方案中存在的 `messages` 表作为权威源。

启动 Recovery Reconciler：

1. 扫描 RUNNING、CANCEL_REQUESTED、WAITING_APPROVAL 和 WAITING_FILES Run。
2. 查询 LangGraph 最新 State Snapshot。
4. 将 CANCEL_REQUESTED 收敛到 CANCELLED；清理或关闭关联的等待任务。
5. RUNNING 默认重新调度；Tool Node 先核对 Ledger。
6. 重建文件 Worker 和审批过期调度。

### 16.2 故障保证矩阵

所有跨外部副作用的步骤必须携带稳定的 `transition_id`、`approval_id`、`tool_call_id` 或其他业务幂等键，使 Reconciler 能区分“尚未执行”和“已经执行但尚未确认”。同一 `athena.sqlite` 事务中的业务状态、Command 状态、Durable Event 和对应 Checkpoint 必须原子提交。

| 故障点 | 可观察的数据现象 | Reconciler 动作与保证 |
|---|---|---|
| Gateway 创建 `message.submit` 的事务提交前退出 | 不存在 Command 和 Run，或二者均不可见 | 客户端使用相同 `command_id` 重试，Gateway 重新生成并原子写入；不会留下孤立 Run |
| Gateway 已提交 Command/Run，但返回 HTTP 前退出 | 已存在唯一 `command_id`、固定 `run_id`，客户端未收到响应 | 重试读回第一次记录及同一个 `run_id`；不得创建第二个 Run |
| Command 被 CLAIMED 后、执行前退出 | Command 为 CLAIMED，Run 尚未推进或仍为 QUEUED | 单消费者模型下保留 CLAIMED 状态，需人工处理或后续显式恢复机制介入 |
| Graph 普通节点保存 Checkpoint 后、业务状态/Event 提交前退出 | Checkpoint 带稳定 `transition_id`，Application 状态落后 | 根据 Checkpoint transition marker 幂等补写状态和 Durable Event；Event 表对 transition key 建唯一约束 |
| 业务状态/Event 已提交、Command ack 前退出 | 业务状态和 Event 一致，但 Command 仍为 CLAIMED | 后续显式恢复机制先检查 transition key；只补 Command ack，不重复状态转换或事件 |
| Approval PREPARING 写入后、Interrupt Checkpoint 保存前退出 | PREPARING Approval 存在，但 Checkpoint 无对应 Interrupt，前端未收到 `approval.required` | 删除或作废 PREPARING 记录，并从上一个安全 Checkpoint 重新执行 approval gate |
| Interrupt Checkpoint 已保存、Approval PENDING/Event 事务提交前退出 | Checkpoint 有 `approval_id` Interrupt；Approval 为 PREPARING 或不存在，且无 `approval.required` | 以 `approval_id` 幂等创建或激活 PENDING Approval，并在同一事务中更新 Run、写 `approval.required` |
| Approval PENDING/Event 已提交后退出 | Checkpoint、PENDING Approval、WAITING_APPROVAL Run 和 Event 一致 | 重建超时调度，继续等待决定；不重复发布 required Event |
| Approval 决定已提交、`Command(resume=...)` 保存新 Checkpoint 前退出 | Approval 已 APPROVED/DENIED，resolve Command 未完成，Graph 仍停在对应 Interrupt | 后续显式恢复机制用已持久化决定再次 resume；条件更新阻止第二个决定 |
| Tool 外部调用前退出 | Ledger attempt 为 PENDING，尚无外部调用开始标记 | 将该 attempt 标为取消或失败，创建新 `tool_execution_id` 的下一 attempt |
| Tool 调用中退出，副作用结果未知 | Ledger attempt 为 RUNNING，无可靠完成结果 | 可安全重试的工具创建新 attempt；不可安全重试的工具创建人工核对 Interrupt，不自动重放 |
| Tool 已将 SUCCEEDED 结果写入 Ledger、Checkpoint 尚未接收结果时退出 | Ledger 有完整成功结果，Graph Checkpoint 仍位于 Tool Node | 按 `tool_call_id` 和 fingerprint 复用结果并推进 Graph；不得再次调用工具 |
| 最终 Snapshot 已提交、`message.completed` 尚未提交时退出 | Snapshot 为 final，消息/Run 仍非 terminal | 根据 `stream_id` 和 transition key 补写最终消息、terminal Event 与 Run/Command 状态 |
| 最终消息、Run、Event 和 Command ack 的事务中途失败 | 整个事务不可见，原状态保持 | 从 Checkpoint 或 final Snapshot 重试同一原子转换；Session 内 `session_seq` 不被消耗 |
| CANCEL_REQUESTED 持久化后、Graph 到达安全点前退出 | Run 为 CANCEL_REQUESTED，Checkpoint 仍在上一个安全点 | 启动时继续执行取消 gate，收敛为 CANCELLED，不自动继续业务节点 |
| Realtime Queue 阻塞期间客户端断开 | Subscription 被取消；Durable Event/Snapshot 已持久化到各自最近提交点 | 释放阻塞生产者；重连通过 Durable replay 和 Snapshot 收敛，不伪造已丢失的未 flush Delta |
| Runtime 在 Snapshot flush 前异常退出 | Checkpoint 存在，Snapshot 可能落后于已显示的 Realtime Delta | 丢弃客户端未持久化尾部，按 Snapshot 对齐；恢复时允许重新执行当前 LLM Node并创建新的 stream |

旧的基于消息历史和恢复提示词猜测恢复点的 SessionRecovery 不再保留；Checkpoint 与业务 Event 的不一致由 Reconciler 补偿。

## 17. SQLite 数据模型

核心新表：

- `agent_runs`：`run_id`、`session_id`、`created_by_command_id`、status、pause/cancel flag、错误和时间。一个 Run 可关联多个 Command，Command 与 Run 的多对一关系由 `agent_commands.run_id` 表达。
- `agent_commands`：Envelope、payload hash、status、attempt、available time、result/error。
- `agent_events`：`session_id` 与 Session 内连续 `session_seq`（联合主键或唯一约束）、event type、session/run、payload 和时间。若需要跨 Session 的物理排序，可另设内部 `row_id`，但不得将其作为 SSE 游标。
- `stream_snapshots`：`stream_id PRIMARY KEY`、version、content、length、status 和时间。
- `approvals`：pending decision、version 和条件更新字段。
- `tool_executions`：工具执行账本。

关键约束和索引：

```text
agent_commands.command_id UNIQUE
agent_commands(status, available_at)
agent_events PRIMARY KEY(session_id, session_seq)
agent_events(run_id, session_seq)
stream_snapshots update WHERE old_version < new_version
同一 session 同时只允许一个非终态 Run（SQLite partial unique index，覆盖 `QUEUED`、`RUNNING`、`CANCEL_REQUESTED`、`WAITING_APPROVAL`、`WAITING_FILES`）
agent_events 永久保存，不设置 TTL、自动清理或按 Session 删除策略
```

消息、步骤、记忆、MCP、工具治理、附件、文件分块和 artifact 按新 schema 重建。允许清库不代表省略 schema、并发和恢复测试。

## 18. Web 与认证

### 18.1 Web 构建

- 保留 React、TypeScript、Vite、Zustand 和现有 UI 组件。
- 移除 Electron main/preload、electron-vite 和 electron-builder。
- 前端目录可从 `desktop/` 改名为 `web/`。
- 开发期 Vite 使用 `/api` proxy；生产由 FastAPI 同源托管 `index.html`。
- API base 使用相对路径，不硬编码 `127.0.0.1:8000`。
- `useWebSocket` 替换为 `useSessionEventStream`；上行全部使用 HTTP Command。

保留页面：Chat、Sessions/Session Detail、Memory、Tools、Approvals、Providers、MCP、Settings、Files/Attachments。Evaluation 页面、导航、Store、反馈 UI 和 API client 方法删除。

### 18.2 `.env` 单用户认证

```dotenv
AUTH_ENABLED=true
AUTH_USERNAME=athena
AUTH_PASSWORD=change-me
AUTH_SESSION_SECRET=<至少 32 字节随机值>
AUTH_SESSION_TTL_HOURS=24
```

`POST /api/auth/login` 校验环境变量用户名和密码，使用常量时间比较，成功后签发带过期时间和签名的 HttpOnly Cookie。`POST /api/auth/logout` 清除 Cookie。REST、SSE 和 SPA 使用同一 Cookie 会话；密码、Session Token 和 Cookie 不进入 URL、SSE data 或日志。

Cookie 设置：

```text
HttpOnly=true
SameSite=Strict
Path=/
Secure=HTTPS 时 true
```

启动安全规则：

- 监听 `127.0.0.1` 时允许显式 `AUTH_ENABLED=false`。
- 监听 `0.0.0.0`、IPv6 非 loopback 或其他局域网地址时强制启用认证。
- 局域网监听时缺少用户名、密码或 session secret 拒绝启动。
- CORS 只允许同源和显式开发 Origin，不使用 `*`。
- 修改状态的请求校验 Origin；登录端点加简单失败速率限制。

公开访问范围仅包括登录页所需静态资源、`POST /api/auth/login` 和最小健康探针。其他 REST、SSE、SPA 路由和文件内容均要求有效 Cookie。静态 SPA fallback 必须在 API 路由之后注册，不能把 API 404 错误错误地返回为 `index.html`。

局域网 HTTP 无法防止同网段窃听，生产部署建议由 Caddy/Nginx 提供 HTTPS。

### 18.3 目标 API 表面

| Method | Endpoint | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 建立单用户 Cookie Session |
| POST | `/api/auth/logout` | 注销 |
| GET/POST/PATCH/DELETE | `/api/sessions` | 会话元数据管理 |
| POST | `/api/sessions/{id}/runs` | 提交 `message.submit` Command |
| POST | `/api/runs/{id}/cancel` | 请求协作式取消 |
| GET | `/api/sessions/{id}/events` | SSE replay + live feed |
| GET | `/api/commands/{id}` | 查询异步 Command 状态 |
| POST | `/api/approvals/{id}/resolve` | 提交审批决定 Command |
| GET | `/api/sessions/{id}/messages` | 会话持久消息投影 |
| GET | `/api/sessions/{id}/runs` | Run 状态与历史 |

Memory、Tools、MCP、Providers、Settings 和 Files 保留现有 REST 资源风格；凡会驱动活动 Graph 的写操作内部转为 Command，普通配置或资源 CRUD 不必为了形式统一全部进入 Runtime Queue。

## 19. Evaluation 完整移除

删除 `athena/evaluation/`、Evaluation API router、RuntimeContainer/Settings 中的 evaluation 成员、仅用于 Evaluation 的 trace sink/shadow runner、Web Evaluation 页面和组件、Evaluation types/Store/API client、专属测试/fixtures/CLI/文档入口及数据目录约定。

保留生产 retrieval、memory、file search、reranking 和普通结构化日志。删除前建立 `rg evaluation` 引用清单；删除后除历史说明和负向架构测试外不应存在引用。

## 20. 功能保留矩阵

| 当前能力 | 新实现 | 状态 |
|---|---|---|
| 多轮会话 | Session + LangGraph Thread | 保留 |
| LLM 流式输出 | Coalescer + Realtime + Snapshot | 保留 |
| Provider fallback/retry | Runtime LLM service | 保留 |
| 预算和上下文压缩 | Graph State + Runtime services | 保留 |
| 工具治理与 MCP | 现有 Catalog/Manager/Adapter | 保留 |
| 审批 | Interrupt + Approval table | 重写保留 |
| 并行工具与子 Agent | Graph fan-out/join/subgraph | 重写保留 |
| 取消和崩溃恢复 | cancel gate + Checkpoint + Reconciler | 重写保留 |
| 记忆与文件智能 | 现有 ports/repositories + Graph nodes | 保留 |
| Evaluation | 无 | 完整移除 |

## 21. 分阶段实施方案

每阶段都必须保持主分支可运行，并独立提交、测试和回滚。不要在同一次提交中同时删除旧路径和引入未经验证的新路径。

### Phase 0：基线与契约冻结

工作：列出功能/回归用例；冻结旧事件到新 Application Event 的映射；记录后端测试、Web typecheck/build；添加 boundary test；锁定 LangGraph/checkpointer 版本。

验收：功能矩阵每项都有测试路径；Contracts 不 import 其他层；无业务行为变化。

### Phase 1：完整移除 Evaluation

工作：按第 19 节删除后端、前端、配置、运行时依赖、fixtures、测试和文档入口；保留生产 retrieval。

验收：后端测试与 Web 构建通过；`/api/evaluation/*` 不注册；UI 无 Evaluation；检索/记忆/文件回归通过。

回滚：独立提交，可整体 revert。

### Phase 2：认证与纯 Web 外壳

工作：实现 `.env` 登录、Cookie、启动 bind guard、受限 CORS；将 Electron renderer 迁为 Vite Web entry；API base 改相对路径；暂时保留 WebSocket 仅用于验证 Web 外壳。

验收：浏览器登录后可使用所有保留页面；未认证 API 和流端点拒绝；LAN 缺认证配置启动失败；Electron main/preload 不再是运行前提。

### Phase 3：Application Contracts 与 EventPublisher

工作：建立 Commands/Events/Ports；把 Workflow、Harness、Approval、Recovery、File Runtime 的 WebSocket 依赖替换为 EventPublisher；用临时 WebSocket Adapter 保持旧前端可用；增加 schema/mapping 测试。

验收：Runtime/core 不 import Gateway；所有 DTO JSON round-trip；旧 UI 仍可收事件。

### Phase 4：SQLite Command/Event/Snapshot 基础设施

工作：创建新 schema；实现 Command enqueue/dedupe/claim/ack/retry；Durable Event Writer；Snapshot versioned upsert；InProcess wakeup/RealtimeTransport；并发、崩溃和 lock 测试。

验收：重复 command 不重复 Run；payload 冲突返回 409；Session 锁竞争返回 `409 session_busy`；paused Run 后的新消息创建新 Run；publish 返回时事件可查；慢 subscriber 只能阻塞其所属 Session，不能阻塞其他 Session。

### Phase 5：LangGraph Runtime

按小步实现：无工具多轮 Graph；工具 loop/预算/retry；并行工具和 Ledger；Approval Interrupt/Resume；Pause/Resume；Memory/Compression；Files waiting；子 Agent subgraph；Recovery Reconciler。

组合根提供短期 `AGENT_RUNTIME_ENGINE=legacy|langgraph` 开关，但不双写 Checkpoint。验收完成后默认 LangGraph 并删除 legacy Workflow/Harness/SessionRecovery。

验收：同一 Session 复用同一 Thread 且 Run 串行；重启能恢复 running/approval/files/paused；暂停后可恢复且期间新消息创建新 Run 并按序执行；终态或 paused 状态后才能创建新 Run；成功工具不无条件重复。

### Phase 6：SSE Gateway 与 Web Client

工作：实现 SSE、Durable replay、Snapshot bootstrap、live feed、heartbeat、gap/resync；Web 使用 `useSessionEventStream` 和幂等 projection；所有上行操作改 HTTP Command；断网、多标签页、慢客户端和重连测试。

验收：生产前端无 WebSocket；断线期间 Runtime 继续；重连后 Durable Event 不丢不重，文本通过 Snapshot 收敛；多标签页一致；替换 SSE Adapter 不影响 Runtime 测试。

### Phase 7：删除 Legacy WebSocket、Electron 和自定义 Runtime

删除 `athena/gateway/ws`、旧 ClientEventType、Electron main/preload、electron-vite/packaging、legacy runtime、feature flag 和兼容映射。更新 README、启动脚本、依赖和架构文档。

验收：无生产 WebSocket/Electron/legacy runtime；`./start.sh start` 启动单进程 Gateway + Runtime + Web；核心回归通过。

### Phase 8：局域网可靠性验收

执行 WAL/长输出/并发 Tool/SubAgent 压力测试；在 LLM、Tool、Event commit、Snapshot、Approval 位置注入退出；测试浏览器断网、睡眠、多标签页；验证 Cookie、Origin、日志脱敏和 LAN bind guard；用 Playwright 验证核心页面。

验收：无无法解释的非终态 Run；Event replay 和 Snapshot 正确；无持续 locked 或无界 WAL；未认证局域网客户端不能访问数据、SSE 或命令；上传、审批、取消端到端通过。

## 22. 测试策略

单元测试覆盖 schema/version、command dedupe/retry、event classification、coalescing/offset/snapshot、Graph reducer/routing/cancel gate、Tool Ledger、认证和 bind guard。

集成测试覆盖 HTTP -> Command Store -> Consumer -> Graph -> Event Store、Interrupt -> resolve -> resume、cancel -> checkpoint -> restart、文件处理节点、replay/live handoff 和多个 subscriber 背压。

端到端测试覆盖登录、创建会话、流式回复、双标签页审批竞争、断网重连、生成中取消、文件自动继续及所有保留管理页面；同时断言 Evaluation API 和页面不存在。

## 23. 默认容量参数

```text
Uvicorn workers                 1
SSE heartbeat                  15s
Realtime coalesce              50ms or 512 bytes
Snapshot flush                 500ms or 50 tokens or 4KB
Durable writer batch           100 events or 50ms
Command notification fallback  250ms
Realtime subscriber queue      256 events
SQLite journal_mode            WAL
SQLite busy_timeout            5s
```

## 24. 风险与完成定义

主要风险包括单进程故障连带影响、SQLite 写竞争、Replay/live 空窗、Realtime 丢 delta、工具副作用重复、审批竞争、取消延迟、Checkpoint 过大、`.env` 明文密码和 Evaluation 删除误伤生产检索。对应措施分别是可替换 Transport、单 DB/单 writer、先订阅再 replay、offset + Snapshot、Tool Ledger、条件更新、显式 cancel requested、最小 State、权限/HTTPS 和独立回归阶段。

只有同时满足以下条件才算完成：Web 是唯一前端；Gateway/Runtime 只共享 Contracts；Runtime 不知道 SSE；`session_id == thread_id`；同一 Thread 下 Run 串行且同一时刻最多一个非终态 Run；Command 幂等且可重投；Durable Event 永久保存、replay 和 Snapshot 收敛；多客户端一致；工具、审批、取消、子 Agent、记忆、文件、MCP、Provider、Settings 全部回归通过；Evaluation 完整移除；本机可关闭认证而 LAN 强制认证；后端、Web build/typecheck 和关键 E2E 全部通过。
