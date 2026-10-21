# Athena 任务运行链路查看方案

> 状态：前端功能实施设计稿  
> 目标：不引入 OpenTelemetry、LangSmith、collector 或其他第三方追踪组件，只利用现有 PostgreSQL 运行数据、`agent_events` 事件和 SSE，在前端查看一次任务的完整执行链路。  
> 依据：当前 `backend/src`、`frontend/src` 和数据库 ORM 模型。

## 1. 产品目标

在会话详情中增加“运行链路”视图。用户选择一个 `run_id` 后，可以看到：

- 运行从排队、开始到完成/失败/取消的完整生命周期；
- Root graph 实际经过的节点和每个节点耗时；
- LLM 调用、重试、验证失败和最终结果；
- 记忆、知识库、文件和图上下文的召回过程，以及最终注入的候选；
- 计划、Worker、任务依赖、重试和汇总；
- 工具调用、审批、超时、拒绝、失败和输出摘要；
- 失败发生在哪一个阶段，以及可以安全展示的错误详情；
- 页面刷新、重新进入会话或 SSE 断线后仍能恢复历史链路。

这里的“trace”是 Athena 自己的**持久化运行链路视图**，不是独立的遥测系统。`run_id` 是唯一入口，`agent_events` 是事件事实来源，`retrieval_runs` 是检索详情来源，前端只负责投影、筛选和展示。

## 2. 明确不做的事情

本方案不引入或依赖：

- OpenTelemetry、LangSmith、Jaeger、Zipkin、Prometheus、collector 等组件；
- 外部 trace 服务、网络 exporter 或新的 trace 数据库；
- HTTP 请求跨进程的 `traceparent` 传播；
- 将每个 token、SSE chunk 或 SQL 语句保存为运行链路节点；
- 将完整 prompt、用户原文、完整工具参数、完整工具输出或敏感文件内容默认展示给用户。

后台异步执行继续使用现有 `command_id`、`run_id`、`session_id`、`plan_id`、`task_id`、`tool_call_id` 和 `retrieval_run_id` 关联。只有在业务事件中缺少稳定关联字段时才补字段，不建立另一套 ID 系统。

## 3. 当前代码可直接复用的基础

| 能力 | 当前实现 | 方案中的用途 |
| --- | --- | --- |
| 运行身份 | `RunCommandService.submit` 创建 `command_id`、`run_id`、`message_id` | 运行链路主键和筛选条件 |
| 运行状态 | `agent_runs` 保存 status、request、response、error、root thread | 顶部摘要、终态和失败信息 |
| 命令状态 | `agent_commands` 保存 queued/running/completed/failed 和时间 | 展示排队及执行入口 |
| 事件历史 | `agent_events` 保存 `session_seq`、`run_id`、事件类型和 payload | 链路时间线的主要事实来源 |
| 实时更新 | SSE `/api/sessions/{session_id}/events`，支持 replay 和 `Last-Event-ID` | 运行中实时刷新 |
| Root 事件 | `run.*`、`node.*`、`message.*`、`tool.*` 部分已存在 | Root 生命周期和活动节点 |
| 编排事件 | `plan.*`、`task.*`、`synthesis.*`、`approval.*` | DAG 和 Worker 链路 |
| 检索记录 | `retrieval_runs`、`retrieval_candidates`，已有查询 API 和页面 | 召回详情及上下文注入关系 |
| 前端状态 | Zustand 中已有 steps、toolCalls、orchestration tasks/plans | 活动态兼容；历史链路改由 API 恢复 |

当前实现有几个必须先修正的事实缺口：

1. Root graph 的 `_publish` 把 `payload.node` 写成了事件类型，没有写实际节点名；前端无法区分 `prepare_request` 和 `acquire_context`。
2. `llm.started` 和 `llm.completed` 已定义且前端监听，但当前后端没有稳定地发布；Root/Worker 的 LLM 调用无法回看。
3. `tool.started` / `tool.completed` 当前没有统一的 `call_id` 和 `duration_ms` 约定；前端已有对应处理逻辑，但字段不足会产生孤立步骤。
4. `ContextAcquisitionService` 的检索记录把状态固定为 `succeeded`、耗时设为 `None`，不能解释 provider 部分失败或上下文获取耗时。
5. 没有按单个 `run_id` 查询事件的 REST 接口；SSE 适合实时流，不适合历史详情页初始化。
6. `StepModel`、`ToolCallModel` 虽然存在，但当前没有完整写入路径。第一版不强行启用双写，避免形成两套不一致事实源。

## 4. 目标用户界面

### 4.1 会话详情布局

在现有 `SessionDetailView` 中增加运行选择器和链路面板：

```text
会话详情
├── 运行列表
│   ├── run-1  已完成  12.4 s  2026-10-20 12:30
│   └── run-2  失败     3.1 s  2026-10-20 12:34
└── 运行链路（选中的 run）
    ├── 摘要：状态、总耗时、消息、事件数、失败阶段
    ├── 时间线：按 session_seq 排序的所有关键活动
    ├── 执行树：Root → 节点/LLM/检索/工具/Plan → Worker/Task
    └── 详情抽屉：状态、耗时、输入输出摘要、错误、关联 ID
```

默认打开时间线；执行树用于理解并行关系。两种视图使用同一份 `RunTimeline` 前端模型，不分别维护状态。

### 4.2 时间线语义

每个活动显示：名称、类型、状态、开始/结束时间或相对顺序、耗时、所属 run/task、错误摘要和关联 ID。状态统一为：

```text
queued | running | waiting | completed | failed | timeout | cancelled | skipped
```

事件和活动不是一一对应：`node.started` 与 `node.completed` 合并为一个节点活动；`llm.started` 与 `llm.completed` 合并为一个 LLM 活动；同一 `tool_call_id` 的工具事件合并为一个工具活动。没有结束事件的活动在运行终态到达时标记为 `unknown`，并显示“未收到结束事件”，不能伪造成功。

### 4.3 执行树语义

执行树使用稳定父子关系：

```text
run
├── root node: prepare_request
├── root node: understand_task
├── context.retrieve
│   ├── provider: memory
│   ├── provider: knowledge
│   └── provider: file
├── llm.call
├── tool.execute
├── plan
│   ├── task A / worker
│   ├── task B / worker
│   └── synthesis
└── final response
```

并行任务使用相同 `run_id`，通过 `plan_id`、`task_id`、`execution_generation` 和 `parent_run_id` 归属；树的顺序只用于展示，不代表并行任务串行执行。

## 5. 统一业务事件协议

### 5.1 不新增 trace 表，扩展现有事件 payload

继续使用 `ApplicationEvent` 和 `AgentEventModel`。除非后续性能证明需要，否则不新增 `trace_spans` 表。所有链路关联信息先放在受控 payload 中，减少 ORM 和迁移范围。

每个可展示活动的 payload 使用以下约定：

```json
{
  "activity_id": "node:run_123:acquire_context",
  "activity_type": "node",
  "name": "acquire_context",
  "status": "running",
  "parent_activity_id": "run:run_123",
  "parent_run_id": null,
  "plan_id": null,
  "task_id": null,
  "execution_generation": null,
  "tool_call_id": null,
  "retrieval_run_id": null,
  "call_id": null,
  "started_at": "2026-10-20T04:30:00Z",
  "completed_at": null,
  "duration_ms": null,
  "error": null,
  "summary": null
}
```

字段要求：

- `activity_id`：同一活动的 started/completed/failed 事件必须一致；由业务代码生成，不能由前端用随机 ID 代替。
- `activity_type`：`run`、`node`、`llm`、`retrieval`、`retrieval_provider`、`tool`、`approval`、`plan`、`task`、`worker`、`synthesis`、`message`。
- `name`：稳定的代码/工具/provider 名称，例如 `acquire_context`、`openai:gpt-4o`、`exec_shell`。
- `parent_activity_id`：用于执行树；没有明确父节点时填当前 run 活动。
- `status`：事件发生时的活动状态；终态事件必须补 `duration_ms`。
- `summary`：低敏展示摘要，限制长度；不放完整 prompt、工具参数和工具输出。
- `error`：结构化的安全错误摘要，只允许 `code`、`message`、`retryable`、`stage`，不返回 traceback。

现有 envelope 的 `session_id`、`session_seq`、`run_id`、`message_id`、`parent_run_id`、`transition_id` 保持不变。`activity_id` 等字段先进入 `payload`，避免修改 SSE 顶层协议。

### 5.2 事件目录

| 事件 | 产生位置 | 必填字段 |
| --- | --- | --- |
| `run.started` / `run.resumed` | `RunExecutionService` | run activity、command_id、status、started_at |
| `run.completed` / `run.failed` / `run.cancelled` | `RunExecutionService` | status、duration_ms、error 或 summary |
| `node.started` / `node.completed` / `node.failed` | Root graph `_node` | 实际 node name、activity_id、duration/error |
| `llm.started` / `llm.completed` / `llm.failed` | 统一 LLM 调用封装 | call_id、provider/model、attempt、duration、token 统计 |
| `retrieval.started` / `retrieval.completed` / `retrieval.failed` | Context service | retrieval_run_id、provider 数、selected/injected 数、duration |
| `retrieval.provider.*` | 各 provider 调用 | provider、status、candidate_count、duration、error |
| `approval.required` / `approval.resolved` | Approval service | approval_id、tool_call_id、decision、activity_id |
| `tool.started` / `tool.completed` / `tool.failed` | Root/Worker tool boundary | tool_call_id、call_id、tool name、status、duration |
| `plan.created` / `plan.completed` / `plan.failed` | Root/Scheduler | plan_id、task_ids、status、duration/error |
| `task.started` / `task.retrying` / `task.completed` / `task.failed` | Scheduler | plan/task/generation、worker_run_id、status、duration/error |
| `synthesis.started` / `synthesis.completed` | Scheduler | plan_id、status、duration、summary |
| `message.delta` / `message.completed` | message projection | message_id、stream_id、chunk_id、is_complete |

`message.delta` 仍用于聊天内容，不将每个 delta 算作运行活动；`thinking.*` 默认只作为消息/状态展示，不进入执行树。

### 5.3 事件产生规则

所有事件发布必须满足：

1. 事件写入 PostgreSQL 成功后再广播到进程内订阅者，沿用现有 `PostgresEventRepository.publish` 语义。
2. 关键 started 事件最终必须有对应终态事件；异常、取消和进程中断时由运行终态补充 `unknown` 的前端收敛逻辑。
3. 业务事件发布失败不能吞掉真正的业务异常；发布失败要记录普通日志，但不能把成功运行改成失败。
4. payload 只放前端需要的摘要和关联 ID，统一限制长度和字段白名单。

## 6. 后端最小改造

### 6.1 事件协议与发布器

新增共享的 `ActivityPayload` 构造函数或小型工具模块，避免每个服务手写字段。建议位置：`backend/src/application/events/activity.py`。它只依赖标准 Python 类型，不依赖 FastAPI 或前端。

改造点：

- `graphs/root.py` 的 `_node` 接收 `name`，发布事件时把真实节点名传给 payload；记录 started 时间，在 completed/failed 计算 `duration_ms`。
- `root_workflow.py` 的工具开始/结束事件生成稳定 `activity_id`/`call_id`；异常出口发布 `tool.failed`，而不是只返回一个失败结果。
- `RetryingLLMProvider` 或其上层统一调用点发布 `llm.started`/`llm.completed`/`llm.failed`，每次重试用 `attempt` 字段区分。采集 provider、model、输入/输出 token 数和耗时；不采集原文。
- `ContextAcquisitionService` 为总检索和每个 provider 记录开始/结束、状态、耗时、候选数和失败原因；调用 `record_context` 时传入真实 status、duration 和 `retrieval_run_id`。
- `WorkerSchedulerService` 的 task 事件补齐 `worker_run_id`、`execution_generation`、开始/结束时间、retrying 关系和父活动 ID。
- `RunExecutionService` 的 run 终态事件补齐开始时间、duration 和统一错误摘要。

### 6.2 历史查询接口

新增：

```http
GET /api/sessions/{session_id}/runs/{run_id}/timeline?after_seq=0&limit=200
```

响应：

```json
{
  "run": {
    "run_id": "run_123",
    "session_id": "session_1",
    "status": "completed",
    "created_at": "...",
    "updated_at": "...",
    "duration_ms": 12400,
    "error": null
  },
  "events": [],
  "next_after_seq": 42,
  "has_more": false,
  "links": {
    "retrieval_run_ids": [],
    "plan_ids": [],
    "task_ids": []
  }
}
```

查询规则：

- 先验证 `session_id` 存在，再验证 `run_id` 属于该 session；不允许只凭 run ID 越权读取。
- 从 `agent_events` 过滤 `session_id = ? AND run_id = ? AND session_seq > ?`，按 `session_seq` 升序，`limit` 上限建议 200。
- 事件只返回关键活动事件，或者在 Repository 层过滤 `message.delta`；聊天消息仍由消息接口/SSE 提供。
- `links.retrieval_run_ids` 从事件 payload 的 `retrieval_run_id` 去重得到；前端点击后调用现有 `/api/retrieval/runs/{run_id}`。
- `next_after_seq` 使用最后一条事件的 `session_seq`，不要用时间戳分页，避免同一时间多事件丢失。

建议新增端口方法：

```python
async def list_run_events(
    self,
    session_id: str,
    run_id: str,
    *,
    after_seq: int = 0,
    limit: int = 200,
) -> tuple[list[ApplicationEvent], bool]: ...
```

暂不新增独立 trace repository；历史查询复用 `EventHistoryPort` 或扩展 `RunQueryPort`。这样可以保持 PostgreSQL 为唯一业务事实来源。

### 6.3 数据库改造

第一版只需要：

- 确认 `agent_events(session_id, session_seq)` 主键和 `idx_agent_events_run(run_id)` 能满足查询；必要时增加复合索引 `(session_id, run_id, session_seq)`。
- 不新增 trace 表，不新增外部服务字段，不修改 `ApplicationEvent` 顶层 envelope。
- 若后续证明 payload 查询成为瓶颈，再考虑将 `activity_id`、`activity_type`、`status` 提升为列；第一版先通过事件 payload 完成验证。
- 不依赖 `Base.metadata.create_all` 自动完成已有数据库索引升级；索引变更必须提供 SQL migration。

## 7. 前端实现

### 7.1 类型和 API

新增前端类型：

```ts
type RunActivityType =
  | "run" | "node" | "llm" | "retrieval" | "retrieval_provider"
  | "approval" | "tool" | "plan" | "task" | "worker" | "synthesis" | "message"

type RunActivityStatus =
  | "queued" | "running" | "waiting" | "completed" | "failed"
  | "timeout" | "cancelled" | "skipped" | "unknown"

interface RunTimelineEvent {
  session_seq: number
  event_type: string
  occurred_at: string
  run_id: string | null
  payload: {
    activity_id?: string
    activity_type?: RunActivityType
    name?: string
    status?: RunActivityStatus
    parent_activity_id?: string | null
    parent_run_id?: string | null
    plan_id?: string | null
    task_id?: string | null
    execution_generation?: number | null
    call_id?: string | null
    tool_call_id?: string | null
    retrieval_run_id?: string | null
    duration_ms?: number | null
    summary?: string | null
    error?: { code?: string; message?: string; retryable?: boolean; stage?: string } | null
    [key: string]: unknown
  }
}
```

在 `frontend/src/api/client.ts` 增加 `getRunTimeline(sessionId, runId, afterSeq, limit)`。请求失败时保留已经展示的事件，并显示可重试状态；不能清空整个会话页面。

### 7.2 状态归并

新增 `runTimelineStore`，或者在现有 `chatStore` 中增加按 `run_id` 分组的历史字段。建议独立 store，避免历史链路和聊天流相互清理。

归并算法：

1. 以 `session_seq` 去重；API 初始化和 SSE 增量共用同一 `appendEvents`。
2. 按 `activity_id` 建索引；started 创建活动，completed/failed/waiting 更新活动。
3. 没有 `activity_id` 的旧事件使用确定性 fallback：`event_type + run_id + session_seq`，并标记 `legacy=true`，避免随机 ID 导致刷新后重复。
4. 终态 run 到达时，仍为 running 的活动改为 `unknown`；只对明确的 `run.cancelled` 显示 cancelled。
5. 通过 `parent_activity_id` 构建树；缺父节点的活动挂到 run 根并标记“父节点信息缺失”。
6. 按 `session_seq` 保留原始顺序；活动排序使用 `started_at`，没有时间时使用首次事件序号。

### 7.3 组件划分

建议新增：

- `RunTimelinePanel`：加载、分页、刷新、空状态和错误状态；
- `RunTimelineToolbar`：运行选择、状态筛选、活动类型筛选、显示/隐藏低价值事件；
- `RunTimelineList`：按时间顺序展示活动；
- `RunActivityTree`：展示 Root、节点、计划和 Worker 的父子关系；
- `RunActivityRow`：图标、名称、状态、耗时、序号、关联 ID；
- `RunActivityDetail`：错误、摘要、token 统计、检索入口和任务详情；
- `RunActivityStatus`：统一状态颜色和文案，避免各页面自行判断。

交互要求：

- 默认折叠 `message.delta`、重复的 `node.started` 低级事件，只展示归并后的活动；
- 点击检索活动时跳转或打开现有 Retrieval View，并传入 `retrieval_run_id`；
- 点击 task 时展示 plan、task、generation、worker thread 和依赖；
- 点击工具时显示审批决定、状态、耗时和受限输出摘要；
- 错误详情默认只展示安全摘要，不允许前端从事件 payload 直接渲染 traceback 或未过滤原始输出；
- 活动仍在运行时显示“实时”状态，历史加载完成后显示数据来源和最后同步的 `session_seq`。

## 8. 实时更新与刷新恢复

运行链路加载和聊天 SSE 使用同一条会话事件流，但各自维护游标：

1. 打开 Session Detail 时先请求 timeline API，得到 `next_after_seq`。
2. 已连接聊天 SSE 时，将之后收到的同 session 事件交给 timeline store；只接受属于当前 `run_id` 的事件。
3. 用户切换 run 时不关闭会话 SSE，只切换过滤条件；必要时补请求新 run 的历史。
4. SSE 断线重连由现有 `Last-Event-ID` 机制处理；timeline store 继续按 `session_seq` 去重。
5. API 分页和实时事件存在交错时，仍由 `session_seq` 去重，不能按请求到达顺序覆盖活动终态。
6. 运行结束后再请求一次最后一页 timeline，确保终态事件已经落库；若请求失败，保留当前活动并显示“历史同步未完成”。

## 9. 隐私和数据边界

链路查看功能必须默认安全：

- 事件 payload 只允许白名单字段；工具参数只显示参数名或脱敏摘要，工具输出只显示长度、状态和最多固定字符的预览。
- 不在 timeline API 返回用户完整 prompt、完整附件内容、API key、Authorization、MCP 环境变量、完整 shell 命令或完整模型回复。
- 错误只返回稳定 `code`、安全 `message`、`stage` 和 `retryable`；Python traceback 继续留在本地服务日志，不进入事件和前端。
- `summary` 和 `error.message` 设置长度上限，后端截断优先于前端截断。
- 以 `session_id` 验证访问范围；即使前端传入另一个 session 的 run ID，也必须返回 404 或稳定的 not found。
- `agent_events` 的业务保留策略仍由系统决定；清理历史事件时，前端显示“链路已过期”，不能把缺数据误判为未执行。

## 10. 分阶段实施

### 阶段 A：事件协议补齐

**后端**

- 增加 `ActivityPayload` 工具和字段白名单。
- 修复 Root 节点实际名称、activity ID、开始/结束时间和耗时。
- 补齐 run、LLM、tool、approval、plan/task/synthesis 的关键事件和终态出口。
- 为旧事件保持兼容解析，旧数据没有 `activity_id` 时由前端标记 legacy。

**验收**：普通问答、LLM 失败、工具失败、审批等待、计划并行和取消至少都有可配对的活动事件。

### 阶段 B：历史 timeline API

**后端**

- 扩展 Event History/Run Query port 和 PostgreSQL repository，按 session/run/sequence 分页。
- 增加 `GET /api/sessions/{session_id}/runs/{run_id}/timeline` Controller、DTO 和权限校验。
- 更新 `backend/docs/athena-api.md`，增加响应结构、分页和错误码。
- 为复合索引提供 SQL migration（若现有索引测试证明不够）。

**验收**：接口能读取新旧 run；跨 session 查询被拒绝；分页无重复、无遗漏；空 run 和不存在 run 有稳定响应。

### 阶段 C：前端链路视图

**前端**

- 添加 timeline 类型、API 方法、归并 store 和活动组件。
- 在 `SessionDetailView` 接入运行选择和 timeline panel。
- 支持时间线/树状视图、筛选、详情抽屉、检索跳转和错误展示。

**验收**：页面刷新后完整恢复历史 run；普通、失败、工具、审批、并行 Worker 和检索链路均可阅读。

### 阶段 D：SSE 合并和回归

**前后端**

- 将 SSE 新事件接入 timeline store，统一 `session_seq` 去重。
- 验证断线重连、API 分页与实时事件交错、运行终态收敛。
- 删除或修正当前会误导排障的完整用户原文日志和不稳定的前端随机步骤 ID。

**验收**：运行中实时更新，断线后不重复，结束后不留下永久 running 活动。

## 11. 测试计划

### 后端单元测试

- Activity payload 的必填字段、长度限制、错误白名单和旧事件兼容。
- Root node started/completed/failed 的活动 ID、节点名和耗时。
- LLM 成功、重试、超时、验证失败和取消的事件序列。
- tool/approval 的批准、拒绝、超时、执行失败和取消。
- Context provider 部分失败时总检索状态、provider 状态和 duration。
- timeline repository 的 session/run 过滤、游标分页、终态事件和空数据。

### 前端单元测试

- 同一 `session_seq` 重复输入不会重复活动。
- started/completed/failed 能合并为一个活动；缺结束事件会变为 unknown。
- 并行 task 按 parent/activity 关系建树，不因到达顺序变化而错排。
- 旧事件没有 activity ID 时稳定归并，不因刷新产生重复。
- payload 中的敏感字段不会被活动详情组件直接渲染。

### 集成和手工验收

1. 提交普通问答，查看 Root 节点、LLM 和最终消息活动。
2. 模拟 LLM 第一次失败、第二次成功，确认重试次数和耗时可见。
3. 模拟 memory/knowledge provider 一个失败、一个成功，确认检索活动显示部分失败和最终注入项。
4. 提交需要审批的工具任务，确认等待、批准、执行和完成形成一条链。
5. 提交并行计划，确认 plan、多个 task、retry 和 synthesis 能展开。
6. 运行中刷新页面、断开 SSE、恢复连接，确认事件不重复且最终状态正确。
7. 用不同 session 的 run ID 请求 timeline，确认不会返回数据。

## 12. 发布和回滚

1. 先发布后端事件兼容代码和 timeline API，再发布前端；旧前端忽略新增 payload 字段仍可正常聊天。
2. 新事件字段全部可选，旧事件和旧数据库无需回填；前端对旧数据显示 legacy 活动。
3. 若 timeline API 出问题，可以隐藏入口或回滚前端，SSE 聊天和现有会话功能不受影响。
4. 若事件量或 payload 大小超出预期，先收窄关键事件白名单和 summary 长度，再评估索引或 projection；不直接引入新组件。

## 13. 交付文件清单

后端：

- `backend/src/application/events/activity.py`
- `backend/src/domain/events/*`（事件字段和类型）
- `backend/src/infrastructure/orchestration/langgraph/graphs/root.py`
- `backend/src/application/orchestration/root_workflow.py`
- `backend/src/infrastructure/integrations/llm/llm_provider.py`
- `backend/src/application/runs/context/service.py`
- `backend/src/application/orchestration/scheduler.py`
- `backend/src/infrastructure/persistence/postgres/repositories/_runtime.py`
- `backend/src/interfaces/http/controllers/sessions_controller.py` 或独立 timeline controller
- `backend/src/application/common/responses.py`
- `backend/docs/athena-api.md`

前端：

- `frontend/src/api/client.ts`
- `frontend/src/types/index.ts` 或新增 `frontend/src/types/timeline.ts`
- `frontend/src/store/chatStore.ts` 或新增 `frontend/src/store/runTimelineStore.ts`
- `frontend/src/hooks/useSessionEventStream.ts`
- `frontend/src/components/pages/SessionDetailView.tsx`
- `frontend/src/components/run/RunTimelinePanel.tsx`
- `frontend/src/components/run/RunActivityTree.tsx`
- `frontend/src/components/run/RunActivityDetail.tsx`

完成标准：只通过现有后端和前端代码，输入一个 `session_id + run_id`，即可在刷新后还原一次任务从 Root 到 LLM、检索、工具、计划、Worker、消息和错误的完整业务链路；不需要外部 trace 服务，也不增加第三方运行时依赖。

