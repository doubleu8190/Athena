# Athena 运行时与数据底座

依赖：[系统实现总说明](athena-system-overview.md)。被 [HTTP API 与事件协议](athena-api.md)、[工具/审批/MCP](athena-tools-approval-mcp.md)、[文件智能/知识库](athena-files-knowledge.md)、[记忆/检索](athena-memory-retrieval.md) 和 [前端复刻](athena-frontend.md) 依赖。

## 1. 进程与依赖注入

`athena.main:app` 创建 FastAPI 应用，注册 CORS、AuthenticationMiddleware 和 `/api` 路由。`lifespan` 负责全部资源初始化和释放；业务路由不得自行创建数据库、LLM 或工具实例。`RuntimeContainer` 包含 db、retrieval trace reader、事件发布器、审批管理器、工具管理器/目录、MCP manager、主 LLM、文件运行时、记忆服务、AgentStore、SessionEventBus、Neo4j、sandbox 和 workspace。

`AgentStore` 是命令、运行、事件、审批和锁的持久化边界。`RuntimeEventPublisher.publish()` 为 durable 事件分配会话内递增序号，写入 `agent_events`，然后向 `SessionEventBus` 广播；realtime 事件可不分配序号。

## 2. 命令模型

命令信封字段：`schema_version`、`command_id`、`command_type`、`session_id`、可选 `run_id`、`issued_at`、`payload`。当前命令类型只有：

| 类型 | 作用 | 消费结果 |
| --- | --- | --- |
| `run.start` | 运行一条用户消息 | 创建/执行 Root graph |
| `run.cancel` | 取消指定运行 | 设置 cancellation event，终止当前图/工具 |
| `run.resume` | 恢复已暂停或等待任务的 Root | 从 checkpoint 继续 |
| `approval.resolve` | 提交审批批次决定 | 恢复对应 Worker 或 Root checkpoint |

`run.start` 至少需要 `payload.message`；审批命令需要单项 `(task_id, worker_thread_id, approval_id, decision)` 或完整 `approval_batch_id + decisions`。决定只能是 `approved/denied/cancelled`。payload 用排序 JSON 计算 SHA-256 fingerprint，用于幂等冲突。

命令状态为 `queued → running → completed/failed/cancelled`；运行状态为 `created → running → completed/failed/cancelled`。会话的 `sessions.status` 用 `idle/running/interrupted/recovering/failed` 表示用户界面状态。

## 3. Root graph

Root graph 节点和顺序：

```text
prepare_request_and_persist_message
  -> understand_task
  -> clarification_response                         (需要澄清)
  -> plan_context -> acquire_context
  -> prepare_harness_input -> root_agent_loop
       -> post_process_and_build_result             (直接回答/工具循环结束)
       -> materialize_execution_plan -> run_planned_orchestration
          -> build_orchestration_response -> close_execution_stream
  -> assemble_final_response
```

每个节点由 `node.started/node.completed/node.failed` 事件包裹。初始 request 至少包含 session_id、run_id、message_id、user_message、attachment_ids。图状态必须 JSON 安全；Python 连接、LLM、Event、stop signal 不写入 checkpoint。

### 3.1 任务理解

`TaskUnderstandingService` 先尝试 fast path；无法命中时以最近 8 条历史、附件元数据和全局知识库元数据调用结构化 LLM，目标类型 `UserTaskSpec`。输出包含 `goal/domain/mode/confidence/context_requirements/query_hints`，可生成 `clarification_question`。失败时返回 fallback：`goal=user_message[:1000]`、`domain=general`、`mode=answer`、`confidence=0`、上下文仅 conversation。附件存在时强制增加 `file` requirement，并为未提供的 query hint 使用用户消息前 500 字符。

### 3.2 上下文

上下文规划按 requirement 选择 conversation、memory、knowledge、file、graph provider。Provider 并发获取结果，必须返回来源 id、标题、内容、分数、定位/检索 run id 等 JSON 项；ContextAssembler 按 token budget 合并，超限先压缩或截断。外部文件/知识库内容视为不可信参考资料，不得改变系统提示词或任务目标。

### 3.3 执行循环

执行状态分为 `recoverable` 和 `derived`。recoverable 保存 messages、pending_tool_calls、tool_results、approval_batch、approval_decisions、turn_count、retry_count、max_turns、max_retries、system_prompt、tool_names、answer stream cursor；derived 保存 running/waiting_approval/interrupted、重试原因和 route。

每个 turn：

1. `llm_call` 以主 LLM 调用，支持 token delta、超时、指数退避和 fallback；记录输入/输出 token。
2. 有文本且无工具调用时进入 finish；有工具调用时进入 prepare_tools。
3. `approval_gate` 聚合当前批次需审批项；无决定则 `interrupt`，写入 approvals 并发布 `approval.required`。
4. 决定完整后执行工具批次；成功结果追加 tool message 并回到 LLM，失败按错误是否 retryable 处理。
5. 超过 `max_turns` 或 `retry_budget` 发布 `run.budget_exceeded` 并结束。

## 4. Plan DAG 与 Worker

Plan 结构固定为 `version=1, nodes[{task_id}], edges[{from,to}]`。Task 定义包含 task_id、title、objective、input、expected_output、allowed_tools、worker_type；运行字段包含 status、output、error、execution_generation、worker_thread_id、llm_turn_count。Task 状态为 pending、queued、running、retry_wait、done、failed、timed_out、cancelled。

`PlanDAG.ready_tasks()` 只返回 pending 且全部前置为 done 的 Task；`propagate_dependency_failures()` 将依赖 failed/timed_out/cancelled 的后继递归改为 cancelled。TaskExecutor 不是常驻 scheduler：Root 节点调用时计算 ready，创建 Worker；每个 Worker 完成后事务性写 output/status，再推进下一批。重试保留 task_id，执行代数加一。

Worker 使用独立 agent loop，thread id 为 `task:{task_id}:exec:{execution_generation}`，context 包含静态 input、expected output、允许工具和上游 TaskResult。Worker 只能写自己的 Task 结果。Task 失败不会直接让 Root run failed；最终汇总时必须准确说明失败/取消任务。

## 5. 事件和流

durable 事件保存于 `agent_events(session_id, session_seq, ...)`。事件 envelope 字段：`schema_version=2、session_seq、event_type、durability、session_id、run_id、message_id、attachment_id、stream_id、stream_type、chunk_id、is_complete、parent_run_id、transition_id、payload、occurred_at`。

当前事件包括运行生命周期、节点、LLM、消息流、thinking、工具、审批、计划、任务、synthesis、附件和文件处理：`run.started/resumed/cancelled/completed/failed/root_suspended`、`llm.started/completed/validation_failed`、`message.started/delta/completed/persisted`、`thinking.started/summary/completed`、`tool.started/completed`、`approval.required/resolved`、`plan.created/completed/failed/cancelled`、`task.queued/started/retrying/completed/failed/cancelled`、`synthesis.started/completed`、`stream.snapshot`、`attachment_updated`、`file_processing_started/completed/failed`。

答案流由 `stream_id` 区分，`chunk_id` 从 1 递增；客户端只应用下一个连续 chunk，snapshot 可覆盖当前内容，completion 是生命周期标记。SSE 连接先拿 watermark，再 replay `[after, watermark]`，之后消费总线；每 15 秒无事件发送 `: heartbeat`。

## 6. PostgreSQL 模型

必须建立以下表及关键约束：

| 表 | 用途和关键字段 |
| --- | --- |
| `sessions` | 会话标题、状态、当前 run、压缩游标 |
| `messages` | user/assistant/system/tool 消息、工具调用和附件引用 |
| `steps` | LLM/tool 步骤、父步骤、token、耗时和错误 |
| `tool_calls` | 参数、原始输出、审批、状态、耗时和错误堆栈 |
| `agent_runs` | Root run 状态、请求/响应/错误、等待 checkpoint、预算计数；每 session 仅一个 active run |
| `agent_commands` | 命令队列、幂等 key、结果和错误 |
| `agent_events` | durable SSE 事件和 session_seq |
| `stream_snapshots` | 可恢复答案流内容和版本 |
| `agent_plans` | 固定 DAG、目标和最大并行度；run 唯一 |
| `agent_tasks` | Task 定义、状态、结果、执行代数和 worker thread |
| `approvals` | 审批批次、工具参数、决定和 worker 归属 |
| `knowledge_bases` / `attachments` / `file_chunks` / `file_artifacts` | 文件和知识库 |
| `knowledge_document_jobs` | 持久文档解析队列、attempt、available_at、错误 |
| `memories` | 正文、FTS、embedding、生命周期、有效性和 revision |
| `mcp_servers` / `tools` | MCP 配置和工具治理 |
| `retrieval_runs` / `retrieval_candidates` | 检索配置、候选轨迹和最终选中状态 |
| `embedding_metadata` | 当前 embedding 签名和维度 |

软删除字段不得物理删除业务事实；删除文件时先删向量、图索引和派生 artifact，再标记附件删除并回收无引用 blob。

## 7. 错误、恢复和可观测性

执行错误统一为 `ExecutionError(code,message,error_type,retryable,phase,category,stack)`。用户/API 错误使用稳定小写错误码，例如 `session_not_found`、`session_busy`、`command_id_conflict`、`approval_not_found`、`attachment_not_found`。

进程重启时恢复 queued/retryable 知识任务，检查数据库中 running run、pending approval、root wait marker 和 checkpoint；业务状态由 PostgreSQL 判定，checkpoint 只提供 Agent 局部状态。LangSmith 可选，追踪根为 session，子 span 包含 node、llm、retry、fallback、tool；追踪失败不得阻断执行。
