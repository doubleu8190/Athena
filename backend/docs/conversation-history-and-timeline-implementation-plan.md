# 对话历史与时间线改造实施方案

**状态：** 核心代码已实施；静态校验通过；数据库结构与当前 ORM 模型保持一致；运行路径验证待执行

**日期：** 2026-10-23

## 1. 目标

解决聊天记录与执行事件分开展示造成的时间顺序错误，并确保后续 Run 能把完整的用户/助手对话历史注入 Root LLM。同时为 Worker 的工具调用保留可恢复、可审计的独立消息轨迹。

本方案保留已确认的行为：工具调用的“开始”和“完成”是两个独立时间点、两个独立时间线条目。

## 2. 已确认的现状与问题

1. `RunRequestPreparationService.prepare` 只将当前用户消息写入 `messages`。Run 成功后，助手正文进入 `agent_runs.response_json`、`memory_processing_jobs.assistant_text` 和完成事件 outbox，但没有作为 `ASSISTANT` 行写入 `messages`。下一轮 Root 从 `messages` 加载历史，因此直接对话上下文缺少历史助手答复。
2. `load_history` 请求最多 100 条消息，而 `PostgresMessageRepository.list_by_session` 先按时间升序、再应用 limit；会话超过 100 条后，返回的是最早的 100 条，而不是最近 100 条。
3. `Chat.tsx` 按 request/processing/response 阶段分组，把执行时间线固定放在用户消息之后或助手答复之前；渲染顺序不由统一时间轴决定。
4. `tool.started` 和 `tool.completed` 当前映射成相同的 `tool:{tool_call_id}`，store 会在原位置更新条目。完成时间虽更新，开始条目的位置仍保留，因此两者不能作为独立时间节点排序。
5. `WorkerGraphState` 已有 checkpoint state，Worker 工具操作也有 `worker_tool_operations` 幂等账本；但它们不构成一份覆盖完整 LLM 交互、可按顺序读取的 Worker 消息轨迹。Worker 的 system/user 输入、assistant 回复和 tool 结果都不能写进会话 `messages`，否则 Root 会把内部执行上下文混入用户对话。

主要代码位置：

- 请求与 Root 历史注入：`backend/src/application/runs/request_preparation.py`、`backend/src/application/orchestration/root_workflow.py`
- 对话消息持久化与查询：`backend/src/domain/sessions/messages.py`、`backend/src/infrastructure/persistence/postgres/models/session_models.py`、`backend/src/infrastructure/persistence/postgres/repositories/message_repository.py`
- Run 完成、答案 outbox 和记忆任务：`backend/src/infrastructure/persistence/postgres/repositories/run_repository.py`、`backend/src/infrastructure/persistence/postgres/models/runtime_models.py`
- Worker 执行、工具账本和结果引用：`backend/src/application/orchestration/worker_workflow.py`、`backend/src/application/orchestration/worker_tools.py`、`backend/src/infrastructure/persistence/postgres/models/tool_operation_models.py`、`backend/src/application/orchestration/worker_results.py`
- 聊天展示与事件归一化：`frontend/src/components/Chat.tsx`、`frontend/src/components/ExecutionTimeline.tsx`、`frontend/src/hooks/useSessionEventStream.ts`、`frontend/src/store/chatStore.ts`

## 3. 目标数据边界

### 3.1 用户对话消息：`messages`

`messages` 只保存面向 Root/用户的会话消息：

- 当前用户输入：`role=USER`，继续在 Run 开始时幂等保存。
- Run 成功后的最终助手答复：`role=ASSISTANT`，每个成功 Run 一条，写入 `run_id` 和服务端完成时间。
- Root 历史只读取 `USER`、`ASSISTANT`。Worker 内部消息、工具正文和工具结果不得进入此表。

最终助手消息使用稳定 ID（建议 `assistant:{run_id}`），与当前 Run 状态、完成 outbox、记忆任务在同一 PostgreSQL 事务中插入；重复完成或恢复执行时以该 ID 幂等去重。只有成功完成且存在用户可见正文的 Run 才写最终答复。失败/取消 Run 不合成助手答复。

Run 的答案增量/完成事件使用同一个助手消息 ID；另带 `reply_to_message_id` 指向本轮用户消息，避免把用户消息 ID 当作最终助手消息 ID。

### 3.2 Worker 执行消息：独立 `worker_execution_messages` 表

新增 Worker 专用轨迹表，按一次 Task execution 隔离。它记录 Worker 每次实际 LLM 交互的完整消息序列，而不只记录工具调用。当前 Worker 每次调用模型时会重新生成一组 system/user 输入；因此每次调用都要原样记录这些输入和对应的 assistant 回复。当前适配层把累计工具结果放在下一次 user JSON 的 `tool_results` 字段中，并不向 Provider 发送原生 `role=tool` 消息；因此表要区分“实际 LLM 消息”和“工具结果回执”，不能把后者伪装成 Provider 消息。建议字段：

| 字段 | 用途 |
| --- | --- |
| `message_id` | 稳定主键，防止恢复或重放重复插入 |
| `run_id`, `plan_id`, `task_id`, `execution_generation` | 定位所属执行及重试代次 |
| `invocation_id` | 关联同一次模型请求的输入和回复 |
| `sequence_no` | 同一次 execution 内的严格消息顺序；唯一约束为 `(task_id, execution_generation, sequence_no)` |
| `entry_type` | `llm_message` 或 `tool_result` |
| `role` | `llm_message` 的 `system`、`user`、`assistant` 角色；`tool_result` 不伪造 LLM role |
| `content_json` | LLM 消息的实际输入/输出内容，或工具结果被加入 Worker 上下文的安全投影 |
| `tool_call_id`, `tool_name` | 将 assistant 回复中的工具调用与 tool result 项关联 |
| `operation_key`, `result_ref` | 关联幂等工具操作与大型结果引用 |
| `created_at` | 服务端记录时间 |

记录顺序为：本轮 system 输入、user 输入、assistant 回复，再按实际完成顺序记录工具结果回执；下一次模型调用的 system/user 输入及 assistant 回复继续追加。assistant 回复中的工具调用以结构化数组保留；保存前按 `task_id + execution_generation + tool_round + call_index` 规范化稳定 `tool_call_id`，同一 assistant 回复可包含多个调用，不能强行拆成多条互不相关的 assistant 消息。工具结果回执各有独立轨迹行并通过调用 ID 和 operation key 关联。下一轮 user 输入仍需原样记录其 `tool_results` 字段，这样轨迹既保留了操作因果顺序，也保留了实际送入模型的上下文。

`worker_execution_messages` 保存模型实际看到/返回的消息轨迹，不代替业务执行状态。Task/execution state 与 checkpoint 继续承载工作流恢复所需状态；`worker_tool_operations` 仍是工具副作用状态与幂等结果的权威账本；`task_execution_results` / `WorkerResultStore` 继续承载 Worker 最终产物和大型结果。各持久层用 execution 身份、`invocation_id`、`tool_call_id`、`operation_key` 和 `result_ref` 关联，避免把状态、模型消息、工具账本混成一张表。

持久轨迹用于审计、诊断和按需恢复；不把所有历史行盲目追加到每次 LLM prompt。当前 Worker 每轮 prompt 由任务目标、输入、上游结果和累计工具结果重建；工具结果仍按执行状态/账本与引用加载，避免重复或过期上下文。模型输入与输出只存储应用实际可见的数据，不尝试保存 Provider 隐藏的推理过程。工具结果正文按模型实际可见的安全预览与 `result_ref` 保存，大型输出继续由 `WorkerResultStore` 保存。

每次 LLM 调用开始时先幂等保存 system/user 输入；模型返回后保存 assistant 回复及规范化的工具调用；操作结果确定后保存 `tool_result` 轨迹项。若进程在工具账本完成后、轨迹项落库前中断，恢复逻辑从账本补写轨迹项，不重复执行副作用。若工具需要审批，审批等待期间写入独立审批事件，不伪造工具结果；批准、拒绝或过期产生确定结果后再追加 `tool_result`。

批准后恢复、进程恢复沿用相同 `execution_generation`；显式重试使用新 generation，建立独立轨迹。该数据不参与 Root 对话历史。

### 3.3 用户可见执行事件：`agent_events` / SSE

在 Worker 工具执行边界发布两个独立事件：

- `tool.started`：在实际调用开始时生成，包含 `tool_call_id`、`tool_name`、`run_id`、`task_id`、`execution_generation` 和开始时间。
- `tool.completed`：操作产生确定结果后生成，包含同一关联标识、完成时间和状态。

两个事件使用不同稳定事件 ID/transition ID，不再共用一个 timeline entry ID。事件只携带 UI 展示所需的安全元数据；凭证、完整 prompt、原始环境变量及大型/敏感工具输出不进入默认事件 payload。结果预览和 `result_ref` 可按现有安全边界呈现。

### 3.4 聊天时间线：统一前端投影

前端建立一份统一渲染列表，元素类型为 `message` 或 `execution_event`。用户消息、助手答复、工具开始、工具完成和其他当前需要展示的执行事件都进入同一排序流程；日期分隔线也从该列表生成。移除依赖 phase group 将时间线固定在某个消息位置的展示方式。

排序规则：

1. 首要排序键使用服务端时间：消息用持久化 `timestamp`，事件用 `occurred_at`。
2. 时间相同时使用事件 `session_seq`，再以稳定 ID 和固定类型优先级保证结果可重复。
3. `tool.started`、`tool.completed` 使用不同 ID，作为两个独立条目保留；收到完成事件不改写开始事件。
4. 发送 `USER_COMMAND` 时将前端乐观消息 ID 一并作为 `message_id` 发送；服务端持久化后发布 `message.persisted`，包含规范 `message_id`、`timestamp` 和 `run_id`。SSE 增量只更新对应助手消息的正文，不重置其首次服务端时间；历史与实时结果通过同一消息 ID 合并。
5. 历史消息与实时事件按稳定 ID 合并去重，避免刷新/重连时重复显示。

## 4. 历史窗口与数据回填

### 4.1 Root 历史窗口

新增“取最近 N 条”查询：数据库先筛选 `USER`/`ASSISTANT`，再按 `timestamp DESC, id DESC` 取最近 100 条，最后反转成升序并排除当前 `message_id`。为该查询添加 `(session_id, timestamp, id)` 组合索引；现有全量历史展示接口仍可继续使用升序查询。

### 4.2 历史 Run 助手答复

历史消息按当前持久化记录读取。当前开发阶段不执行旧 Run 消息回填；已完成 Run 的历史答复不会通过后台脚本补造。

## 5. 实施顺序

### 阶段 A：存储与历史上下文（已实施）

- 扩展消息仓储，增加最近 N 条的倒序取数/正序返回能力。
- 修改 `RunRequestPreparationService.load_history`，只组装用户和助手对话，排除当前用户消息。
- 扩展 Run 完成事务，将最终助手答复与 Run 状态、记忆任务、完成 outbox、命令终态一并幂等写入。
- 统一答案 stream 事件上的助手 `message_id`，并为用户消息持久化确认提供稳定 ID 与服务端时间。
- 当前数据库表结构以 ORM 模型定义为准；新开发库通过 `Base.metadata.create_all` 创建缺失对象。

### 阶段 B：Worker 消息轨迹（已实施）

- 新增 `worker_execution_messages` ORM、仓储和索引，覆盖 system/user/assistant 消息及独立的工具结果回执类型。
- 在每次 LLM 调用前保存其 system/user 输入，返回后保存 assistant 回复；工具账本确定结果后保存对应 `tool_result` 轨迹项。
- 为同一 assistant 回复中的多工具调用保留结构化数组及各自稳定 `tool_call_id`；按 execution sequence 追加输入、输出和结果。
- 将消息轨迹用于审计和恢复对账；模型上下文仍由 Task 状态、上游结果、工具账本及结果引用重建，不把历史消息盲目重复拼入 prompt。
- 实现审批恢复、进程恢复和手动重试时的 generation 隔离与账本结果补写；这些运行路径尚待集成验证。

### 阶段 C：可排序执行事件（已实施）

- 在真实 Worker 工具调用边界发布独立的 started/completed 持久事件。
- 保证重放/恢复时按稳定事件键去重；两个生命周期事件拥有各自的 `session_seq` 和 `occurred_at`。
- 限制事件 payload 为安全展示数据，并保留关联 Worker 消息/操作的 ID。

### 阶段 D：统一聊天渲染（已实施）

- 在前端将持久消息和运行事件归一成统一 timeline entry 类型。
- 用服务端时间排序，并以 sequence/稳定 ID 处理同时间项；重新计算日期分隔线。
- 将工具开始和完成拆成两个节点，移除 phase-based 固定插入位置与同 ID 原位更新。
- 处理乐观消息与持久确认的合并，以及历史加载和实时 SSE 的去重。

## 6. 验收标准

1. 连续两轮对话时，第二轮 Root history 中包含第一轮用户消息和第一轮助手最终答复，按时间升序排列；Worker 内部工具消息不会出现在 Root history。
2. 超过 100 条消息的会话仍把最新 100 条作为模型窗口，窗口内顺序为旧到新，当前请求不会重复注入。
3. 同一 Run 重复完成/恢复只产生一条助手消息、一条记忆任务和一组完成 outbox 事件。
4. 已有 completed Run 的答复能从持久来源幂等回填；重复运行回填不会增加重复消息或覆盖冲突正文。
5. Worker 每轮 system/user 输入、assistant 回复及 tool 结果都按顺序保存在同一个 execution generation；assistant 多工具调用与各 tool 结果可一一关联。审批恢复继续使用原轨迹，显式重试使用新轨迹。
6. 工具开始和完成在 UI 中是两个独立条目，并按各自服务端时间显示；开始后完成不会把开始条目移动到完成时刻。
7. 用户消息、工具事件和助手答复混排时，页面顺序由时间戳决定，不再由 request/processing/response 分组决定；刷新和 SSE 重放不产生重复条目。

## 7. 风险与执行边界

- 回填前需审计 `agent_runs.updated_at`、`response_json` 与 `memory_processing_jobs` 的实际保留/清理策略，以及历史记录中的内容冲突。回填只覆盖可确认的一对一 Run 答复。
- Worker 外部副作用与数据库事务无法做到原子提交；必须依靠现有 `worker_tool_operations` 幂等账本、稳定调用标识和恢复对账来避免重复副作用。完整 LLM 输入可能包含任务数据，需沿用 Worker 执行数据的访问控制和保留策略。
- 时间戳是跨消息表与事件表的主排序依据；同一时间戳时用事件序号和稳定规则打破平局。若后续要求跨表严格单调全序，应另行把消息确认纳入会话统一序列，而不是假设两个表的本地顺序天然一致。
- 实现已新增独立 Worker transcript 仓储。历史 Run 助手答复不做回填。
- 本轮通过 `python3 -m compileall` 和前端 TypeScript 类型检查；未运行测试或数据库集成验证。当前可用 Python 环境未安装 SQLAlchemy，后端数据库仓储无法在本地做运行时导入检查。
