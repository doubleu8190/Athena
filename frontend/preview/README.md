# Athena target UI preview

这是基于 `backend/docs/athena-target-implementation.md` 的独立静态展示页面，不复用或修改当前 `frontend/`。信息架构围绕三个产品任务组织：与 Athena 对话、查看执行结果、管理系统配置。审批是会话/Task 中的阻塞动作；事件是 Run 时间线；模型和工具配置都归 Settings 管理。

## 页面展示的目标能力

- 会话工作区：用户消息、Root Agent 回答、当前 Run 上下文和执行链路。
- Run 生命周期：`running`、Root suspended、DAG 推进、`run.resume`、Root 恢复和最终回答。
- Plan DAG：通过 `nodes + edges` 展示 Task 依赖、并行度和 Ready / Pending 状态。
- Worker 详情：`worker_type`、`execution_generation`、`worker_thread_id`、objective 和结构化 output。
- 内嵌审批：在等待审批的 Worker Task 上展示 tool、风险等级、arguments 与决策动作。
- Run 时间线：在 Run 详情中按业务上下文展示持久化事件，不提供脱离任务的独立事件产品页。
- 系统资源：知识库文档、长期记忆和会话文件；模型 Provider、工具治理与运行策略统一归入 Settings。
- 实现准备度：明确标出真实前端还需要的 API 数据。

## Run 与 Plan 的边界

页面刻意把下面五个阶段分开：

1. Root Agent 提交 Plan，并保存 `run:{run_id}` 等待 checkpoint。
2. TaskExecutor 推进 DAG，Worker 执行各自 Task。
3. 全部 Task 进入终态后，Plan 才是 `completed`；这一步不代表 Run 已完成。
4. Runtime 创建幂等的 `run.resume` Command，payload 不携带 Task 结果。
5. Root 恢复后重新从 PostgreSQL 读取 Plan / Task，生成最终回答，最后 Run 才进入 `completed`。

审批期间只恢复具体的 Worker；取消、重试和失败传播也都保留在 Run 详情。事件用于驱动界面刷新和重连恢复，在 Run 时间线中作为执行脉络呈现。

## 配置持久化

静态原型将 Settings 的编辑值保存在浏览器 `localStorage`，用于验证未保存提示、显式保存、revision 状态和刷新/重启浏览器后的保留行为。真实产品不能把 `localStorage` 当作系统配置事实来源：Provider、模型引用、运行预算、审批策略和工具开关应写入后端 durable settings store（例如 PostgreSQL 配置表或加密的本机配置文件）；密钥应单独保存在 OS Keychain。保存 API 返回 revision 与 `updated_at`，应用启动时从该存储加载配置，活动 Run 使用创建时的配置快照。

## 连接真实后端前还缺少的数据

### 工作台聚合数据

1. 会话工作区需要 `session`、消息列表、当前活动 Run、最近 Runs 和 SSE 连接游标；审批数量只用于相关会话/Run 的阻塞状态提示。
2. Runs 列表需要 `run_id/session_id/request_json/status/created_at/updated_at/root_wait_generation/plan_id/plan_status/task_terminal_count/task_total_count`，以区分直接回答、DAG 执行中、Root 等待和最终完成。
3. 知识库需要 `knowledge_base_id/name/description/document_count/ready_count/total_size/updated_at`，文档需要 `attachment_id/name/mime_type/size/status/chunk_count/error/processed_at`。
4. Memory 需要 `memory_id/kind/content/confidence/source_session_id/created_at/updated_at/revision_count`，检索结果还需要 score 和命中来源。
5. Files 需要 `attachment_id/session_id/name/mime_type/size/status/progress/error/preview_url/created_at`，不能只返回上传成功状态。
6. Settings 读模型需要 Provider、模型引用、Capabilities/Tools 的 `enabled/risk_level/approval_required/allowed_worker_types`，以及 `revision/updated_at`；写 API 必须持久化并在进程重启后重新加载，不能只改前端状态。
7. 密钥只需返回 `configured/key_reference/updated_at`，不得回传明文；设置写入接口需要字段级验证错误和并发 revision 检查。

### Run / DAG 原子数据

8. `GET /v1/runs/{run_id}` 需要返回 `run`、`plan`、`tasks`、`approvals` 的聚合响应；Task 至少包含 `task_id/title/status/output_json/error_json/worker_type/execution_generation/worker_thread_id/llm_turn_count`。
9. `plan.plan_json` 需要原样返回 `version/nodes/edges`，前端据此绘制 DAG；Task 本身不应新增 `depends_on` 字段。
10. Worker 可观测数据需要明确字段：当前节点、turn 数、tool call 数、checkpoint id、等待原因和 artifact 引用。
11. 审批需要 `approval_id/task_id/worker_thread_id/tool_call_id/tool_name/arguments_json/risk_level/status/decision/created_at/decided_at`。
12. SSE 需要返回持久化 `session_seq`、事件类型和包含 `run_id/plan_id/task_id/status/worker_type/execution_generation/error` 的 payload，并支持 `last_seen_sequence` 重放。
13. 大结果需要 artifact API 或可访问的 `artifact_id/kind/location/summary`，否则只能展示摘要。

建议后端提供两个稳定的聚合 read model：`session workspace`（会话、消息、当前 Run 和运行摘要）和 `run detail`（Run、Plan、Tasks、Root resume 阶段、Task 内审批、执行时间线）。Settings 另有版本化读写 API。按钮动作分别调用 `resume`、`cancel`、Task approval、`retry` API，前端用 SSE 刷新同一工作流中的状态。
