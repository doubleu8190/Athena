# Athena Agent 架构 v1.1 具体实施方案

方案编写日期：2026-10-21。实施状态更新至 2026-10-22：P1–P6 核心交付和 A01–A20 可靠性验收已完成；真实第三方 API 的取消联调范围见第 16 节。

后续专项实施（2026-10-23）：[Root 统一决策设计与实施方案（首期收敛版）](root-unified-decision-implementation-plan.md)已完成代码切换及 D4 真实模型、工具、审批和浏览器验收。该专项仅替换前置分类与初始决策链，复用检索和既有执行协议；历史附件、访问统计和初始命令重投另行处理。下文已完成状态不重复统计这项专项改造。

设计依据：用户提供的 `/Users/udouble/Downloads/Athena_Multi_Agent_Architecture_v1.1.md`，全文 770 行、含取消控制通道和验收要求。实现基线：本日当前工作区，包含未提交的代码修改；不以旧目录或历史方案代替当前源码。

## 1. 实施结论与范围

采用 **Root Graph + 确定性 DAG Orchestrator + 独立 Worker Graph**。这是调度职责、持久化模型和恢复协议的整体替换。保留现有上下文、记忆、文件、知识库、Provider、工具适配器和 SSE 基础设施，围绕新的执行链重新接线。

- Root 负责目标理解、初始规划、每轮 Review、Replan、最终回答。
- Orchestrator 独占 Plan/Version/Round/Task/Execution 状态写入权，不依赖 LLM。
- Worker 只接收任务和所需上游结果，运行自己的模型与工具循环。
- Approval Manager 只决定具体工具操作是否获准；通过 Worker Resume Job 恢复对应执行。
- Run Controller 负责用户请求生命周期和独立停止通道；取消不调用 Root Review。

先保持模块化单体部署：API、Orchestrator 消费者、Worker Dispatcher、Root Resume Dispatcher 可以在同一进程中运行，但通过持久化协议交互。独立运行不要求第一步拆成多个服务；双进程并发测试用于验证协议没有依赖内存状态。

本方案沿用已有分层：`domain` 保存纯业务类型和不变量，`application` 保存用例及当前 LangGraph 图，`infrastructure` 保存 PostgreSQL/工具等适配器，`workers` 保存后台消费者，`bootstrap` 只装配。当前 LangGraph 正从 `infrastructure/orchestration` 移入 `application/orchestration/langgraph`，不在本次计划中反向迁回。

新文档覆盖旧版中不一致的契约：Plan 需要 `WAITING_REVIEW/CANCELLING`；完成屏障从整个 Plan 改为当前 Round；新增自动故障修复 reconciler；审批恢复不走 Root `run.resume`。不保留两套活跃调度语义。

## 2. 当前实现与目标差距

以下是静态代码证据，不代表已验证生产行为。

| 位置 | 当前行为 | v1.1 改造 |
|---|---|---|
| `application/orchestration/root_workflow.py:437` | 启动 Scheduler，每 50ms 查询 Plan，最多等待 120 秒 | 提交命令后进入真正的 `interrupt()`；移除计划完成轮询 |
| `application/orchestration/scheduler.py:42,62` | 内存 `asyncio.Task` 执行 Worker；终态后再次计算 ready 并补派 | 固定 Round 成员，持久化 Job；只在 Root 允许后创建下一轮 |
| 同文件 `:146` | Scheduler 可调用结果 Synthesizer，结果保存在内存 | 删除调度层的模型汇总职责，由 Root Review/Finalize 接管 |
| `infrastructure/persistence/postgres/repositories/orchestration_repository.py:68,101` | 根据整个 DAG 领取任务，全部 Task 终态即结束 Plan | 增加 Round 屏障、版本条件和事件事务；Plan 由 FinishPlan 结束 |
| `domain/orchestration/entities.py` | 无 Round、PlanVersion、独立 execution ID/lease；Task 成功为 `done` | 补齐业务实体、身份和状态机；新协议统一成功值 |
| `application/orchestration/langgraph/graphs/root.py:215`、`graphs/worker.py:59` | 恢复传普通 state dict，通过路由重新进入 | 恢复使用同 thread 的 `Command(resume=...)`，核对实际 pending interrupt |
| `application/orchestration/worker_workflow.py` | 有工具批次路由骨架，审批只是状态返回 | 拆 Prepare/Policy/Approval/Execute/Observe 节点，审批使用持久化中断 |
| `application/orchestration/langgraph/runtime/root_agent.py:167` | 生产 Worker Agent 单次调用模型并返回 `done` | 真正的 Agent Loop、工具绑定、预算、结构化成功/失败和证据 |
| `bootstrap/postgres.py:344` | Worker 仅注入模型执行函数，未接完整工具/审批链 | 显式注入工具策略、执行网关、审批校验、租约、取消令牌 |
| `application/approval/service.py:98`、`bootstrap/postgres.py:135` | 决定落库后直接回调，回调执行 Root `RUN_RESUME` | 决定和 ResumeWorker Job 同事务；HTTP 快速返回，后台只恢复该 Worker |
| `application/runs/execution_service.py:47` | 取消 Root 后直接把 Run 置 cancelled；等待命令映射在内存 | 先原子 CANCELLING，再协调 Worker、审批、恢复 Job，收敛后终结 |
| `application/orchestration/langgraph/runtime/worker_executor.py:79` | Worker cancel 当前返回 0 | 持久取消控制 + 本地调用登记 + 协作取消 + fencing |
| `infrastructure/persistence/postgres/models/runtime_models.py` | 有 Run/Plan/Task/Command/Event/Approval；缺少 Round/Version/Execution/可靠投递模型 | 复用主表，新增版本、轮次、执行和队列表；展示事件不当作调度队列 |
| `infrastructure/persistence/postgres/unit_of_work/postgres.py` | 有 UoW 外壳，但仓储通常各开事务 | 增加用例范围共享 session 的事务端口，支撑跨仓储原子写入 |
| `frontend/src/types/index.ts`、`types/events.ts` | Plan 状态未覆盖 Review/取消中，缺少 Round/Version 协议 | 同步类型、投影、审批卡片、停止状态和历史读取 |

现有 `test_multi_worker_e2e.py` 位于 unit 目录、使用替身仓储；不能把名称中的 E2E 当成真实 Provider/PostgreSQL/浏览器验收。

## 3. 编码前固定的实施口径

以下是对文档未完全展开部分的建议默认值，应与实现一并固化到契约测试。

1. **冻结全部 ready，不按并发上限截断 Round。** 例如 10 个 ready、并发上限 3，10 个都是本轮成员，最多 3 个同时运行。完成一个可领取本轮剩余成员，禁止把刚变成 ready 的下游任务补进本轮。
2. **Task 与 Execution 分离。** 某 attempt 失败但仍允许重试时，只终结 attempt，Task 保持非终态，不增加 Round 终态计数。退避时间、次数和原因落库；重试留在原 Round。
3. **依赖失败交给 Review。** 当前 Round 结束后把失败依赖及不可运行的下游列入 ReviewContext，不沿用当前 DAG 的自动级联取消来替 Root 决策。Continue 返回 `BLOCKED_DEPENDENCIES`，Root 必须修订或结束。
4. **版本是不可变快照，执行事实不是快照副本。** 同一个成功 Task 可被多个版本引用；修改目标、输入、依赖或权限必须创建新 Task ID，不覆盖已完成结果。
5. **FINISH 可以提前结束。** 未执行的剩余 Task 记为 `CANCELLED(reason=plan_finished)`，不抹掉成功或失败事实；成功/失败由 Root 对照目标明确判断。
6. **第一版不引入无限期通用 BLOCKED。** 非审批缺输入先作为结构化 `FAILED(code=needs_input)` 进入轮次 Review，由 Root 澄清、修订或结束。未来可另加带期限和恢复契约的阻塞状态。
7. **普通对话和澄清可直接回答。** 需要实际执行工具的工作进入 Plan；单任务也生成一节点 DAG，避免继续维护 Root 执行工具的第二条路径。这是建议的产品范围口径。
8. **审批过期按拒绝处理。** EXPIRED 决定与恢复 Job 同事务，Worker 收到 ToolDenied 后可尝试替代方案；Task 总时限和模型预算仍生效，绝不自动批准。
9. **业务成功状态统一为 `completed`。** 领域枚举名可用 COMPLETED；旧 Task 状态 `done` 映射为 completed、`timed_out` 映射 failed 并保留错误码。历史展示通过只读映射处理。
10. **有限规划预算。** 限制总轮次、重规划次数、Root/Worker 模型 turn、任务数和执行时限，全部来自 Settings。无意义重复 Review、非法模型输出要有有界修复和明确失败路径。

建议状态集合：Plan=`created/running/waiting_review/cancelling/completed/failed/cancelled`；Round=`running/completed/cancelled`；Task=`pending/running/waiting_approval/cancelling/completed/failed/cancelled`。待退避重试用 Task pending + Execution/Job 的 available_at 表达；Execution 单独记录 queued/running/interrupted/completed/failed/cancel_requested/cancelled。Run 在轮次和审批等待时保持 running，额外增加 cancelling，不把 Graph 的 suspended 当成 Run 终态。

## 4. 目标模块与接口

建议在现有目录内扩展，以下新文件为拟新增位置。

```text
backend/src/
  domain/orchestration/
    entities.py                # Plan / Round / Task / Execution / Version
    commands.py                # Submit/Continue/Revise/Finish/Cancel
    events.py                  # RoundCompleted / WorkerEvent 等
    review.py                  # ReviewContext / ReviewDecision
    dag.py, validation.py      # 纯校验和 ready 集合计算
    ports.py                   # 事务、仓储、执行、结果引用端口
  application/orchestration/
    command_handler.py         # 确定性编排命令入口
    round_service.py           # 冻结成员、推进轮次、结束屏障
    task_event_handler.py      # 校验 Worker 事件并推进事实
    review_context_service.py  # 按 round_id 构造一致审查输入
    root_workflow.py           # 规划、审查、提交决策、最终回答
    worker_workflow.py         # Worker 模型/工具循环用例
    langgraph/graphs/          # Root / Worker 图与 interrupt 节点
    langgraph/runtime/         # 调用、pending interrupt 读取、恢复适配
  application/approval/
    service.py                 # 原子决定和恢复作业，不直接调用图
    interrupt_collector.py     # checkpoint 确认后登记审批
  application/tools/
    approval_policy.py         # 工具/参数/资源/用户的确定性策略
    execution_gateway.py       # 权限、审批、取消、幂等统一门禁
  application/runs/
    cancellation_service.py    # 独立停止控制入口
    finalization_service.py    # 最终消息、Run 终态和后处理幂等收敛
  infrastructure/persistence/postgres/
    models/orchestration_models.py
    repositories/              # 状态、命令回执、Job、Outbox/Inbox
    unit_of_work/              # 用例共享事务实现
  workers/
    worker_dispatcher.py
    root_resume_dispatcher.py
    outbox_dispatcher.py
    orchestration_reconciler.py
```

逻辑契约：

- `handle_command(command) -> CommandReceipt`：按业务幂等键返回固定回执；校验状态、版本、review_round_id。
- `apply_worker_event(event) -> EventReceipt`：事件去重、执行栅栏验证、attempt/Task 更新、轮次屏障和 Outbox 同事务。
- `load_review_context(round_id) -> ReviewContext`：读取该轮版本和结果，不漂移到后续版本。
- `claim_job(worker_id) -> JobLease`：SKIP LOCKED 领取、短事务更新租约和 token，事务外调用图。
- `resume_root(job)`、`resume_worker(job)`：核对 thread/checkpoint/interrupt 和取消状态，恢复调用串行。
- `cancel_run(run_id, actor, idempotency_key) -> CancelReceipt`：直接落停止状态；后台控制通知可异步。

跨边界模型显式定义 schema_version；领域保持 dataclass/enum，不依赖 SQLAlchemy/Pydantic/LangGraph。HTTP DTO 和模型输出在入口校验；JSONB 在仓储边界编码，不使用任意 dict 贯穿所有层。

## 5. PostgreSQL 数据模型

物理表优先复用 `agent_*` 和 `approvals`，不用为了对齐文档示例而整体改名。

| 表 | 具体增量与约束 |
|---|---|
| `agent_runs` | 增 cancellation_epoch、控制/终结元数据；状态加入 cancelling；记录 Root wait 的 round/version/interrupt/checkpoint 身份 |
| `agent_plans` | 增 current_version、active_round_id、cancellation_epoch；加入 waiting_review/cancelling；沿用一 Run 一 Plan |
| `plan_versions` | PK(plan_id, version)，不可变 dag_json、目标/约束引用、修订原因、review_round_id、创建命令 |
| `agent_tasks` | 保留逻辑 Task、当前状态、active_execution_id、result_ref；增加定义哈希，旧 execution_generation 仅作 attempt 序号 |
| `plan_version_tasks` | PK(plan_id, version, task_id)，版本成员、定义和依赖引用；FK 指向不可变 Task 定义；不复制执行结果 |
| `plan_rounds` | round_id、plan_id、version、round_no、status、expected_count、terminal_count；UNIQUE(plan_id, round_no) |
| `round_tasks` | PK(round_id, task_id)，冻结成员；每 Task 至多一个实际执行 Round，重试仍在同轮；不可向已启动 Round 增成员 |
| `task_executions` | execution_id、task_id、round_id、attempt、worker_thread_id、state、lease_owner/until、fencing_token、checkpoint_ref；UNIQUE(task_id, attempt) 和 worker_thread_id |
| `approvals` | 增 execution_id、worker_thread_id、interrupt_id、operation_key、args_hash、expires_at、decided_by；UNIQUE(worker_thread_id, interrupt_id)；完整决定状态 |
| `orchestration_commands` | 命令信封、业务幂等键、payload_hash、固定回执；UNIQUE(调用主体或命令作用域, idempotency_key) |
| `worker_jobs` | execute/resume/cancel，执行/中断引用、available_at、attempt、租约、状态；执行派发和审批恢复分别有唯一键 |
| `root_resume_jobs` | event_id、Root wait 身份、租约/重试/状态；UNIQUE(consumer_name, event_id) |
| `outbox_events` | 业务事件信封、接收方、投递状态、可重试时间；RoundCompleted 语义唯一键包含 round_id |
| `inbox_events` | UNIQUE(consumer_name, message_id)，received/processed 状态和结果；先收件不等于已处理 |
| `tool_operations` | operation_key、不可变参数哈希、执行状态、结果引用、外部请求 ID、external_effect_status；UNIQUE(operation_key) |

独立的 orchestration_commands 是 Plan 控制命令回执表；原 `agent_commands` 继续承载 HTTP Run 提交和命令查询，不充当第二套 Plan 状态源。worker_jobs 中的审批恢复以 approval_id/interrupt_id 为幂等身份，不能只保存 approved 布尔值。

结果引用优先指向持久 Task 结果和现有文件 artifact，包含摘要、证据定位、类型和大小；大输出不塞入事件或 Root checkpoint。TaskResult 中的 evidence、uncertainties、metrics 需实际落库，不在转换中丢弃。

新表时间使用 TIMESTAMPTZ/UTC；现有字符串时间字段保留边界转换，避免本次顺带重写全库。

关键数据库约束：

- 每 Plan 最多一个 RUNNING Round；每 Task 最多一个当前有效 execution。
- RoundCompleted 的业务唯一性独立于随机 message_id，重复消息不能再次加 terminal_count。
- 所有归属通过 FK 或受锁事务核验，禁止跨 Run/Plan 引用 Task、审批和结果。
- Run 活跃唯一索引及 `get_active_for_session` 把 cancelling 纳入活跃范围；Root 等轮次期间 Run 仍 running。
- 调度队列为 status/available_at/lease_until 建索引；待审批不按活跃执行租约过期重试。

当前 PostgreSQL schema 以 SQLAlchemy ORM 模型为准。`PostgresEngine` 在 `create_schema=true` 时调用 `Base.metadata.create_all` 创建缺失的表和索引；该调用不会修改已存在表的字段。开发库结构需要与当前 ORM 模型一致。

## 6. 核心事务与严格 Round 算法

所有相关仓储在同一用例事务共享 session。不要先调用一个自行提交的仓储，再调用另一个仓储来模拟原子性。长模型调用、工具调用、等待审批均在事务外。

统一锁顺序：Run → Plan → Round → Task（按 ID 排序）→ Execution → Approval/Job。Worker 终态可先无锁定位归属，再从 Plan/Run 开始加锁；取消与领取遵守相同顺序，设置锁超时和有限事务重试。

### 6.1 Submit / Continue / Revise

同事务执行：命令去重和 payload_hash 核对 → 锁 Run/Plan → 校验可执行状态、版本及待审 round → 验证完整 DAG → 写版本（仅 Submit/Revise）→ 计算全部 ready → 写 Round 及全部成员 → 建立执行记录/派发 Job → 写 Outbox 和固定回执。

- 同幂等键不同 payload 返回冲突；同键重试返回原回执，不用当前状态重新执行。
- Continue/Revise 必须消费当前 WAITING_REVIEW 的 round；一轮只接受一次有效决策。
- 无剩余任务的 Continue 返回 NO_REMAINING_TASKS；剩余任务因失败依赖无 ready 返回 BLOCKED_DEPENDENCIES，不创建空 Round，不伪造 RoundCompleted。
- 无后续任务的修订建议拒绝为 NO_RUNNABLE_TASKS 并要求 FINISH；禁止提交无事件来源的等待状态。
- Round 包含全部 ready，但领取 Job 时才检查并发额度；同轮排队任务可后续启动。

### 6.2 Worker claim / heartbeat / completion

claim 在事务内检查 Plan/Run 未取消、Round 身份和额度，更新 Job、Execution 运行状态与租约。领取后在事务外调用 Worker Graph。

完成事件事务：Inbox 去重 → 核对 active_execution_id、fencing_token、cancellation_epoch → 写 attempt 结果 → 决定持久重试或 Task 终态 → 仅首次 Task 终态增加计数 → 若本轮全终态且 Plan 仍 RUNNING，则完成 Round、Plan=WAITING_REVIEW、插入唯一 RoundCompleted Outbox → 标记 Inbox processed。

正常完成不直接把 Plan 置 completed。取消期间 Task 结果按提交顺序归档，退出确认走取消收敛路径，不触发 RoundCompleted。

### 6.3 Outbox / Inbox / 投递

业务事务写 Outbox；Dispatcher 至少一次投递。投递后、标记完成前崩溃允许重投，由消费者唯一键去重。Inbox received 状态可先落库；只有业务变更或恢复结果已持久化才置 processed，不能先打“已处理”标记再调用图。

短间隔轮询 Job/Outbox 是传输实现，允许使用；禁止通过高频遍历全部 Task 来判断 Round 完成。LISTEN/NOTIFY 可用于唤醒，数据库队列仍是真相来源。

## 7. Root Graph、Review 与恢复

目标图：

```mermaid
flowchart LR
  U[Understand / Context] --> P[Build Initial Plan]
  P --> S[SubmitPlan]
  S --> W[WaitPlanEvent: interrupt]
  W --> L[Load ReviewContext]
  L --> R[LLM Review + Validate]
  R --> D{Decision}
  D -->|CONTINUE| C[ContinuePlan]
  D -->|REPLAN| V[RevisePlan]
  C --> W
  V --> W
  D -->|FINISH| F[FinishPlan]
  F --> A[Finalize / Persist Answer]
```

把 SubmitPlan 与 Wait 拆成不同节点，避免 resume 重跑包含命令提交的节点。即使因 checkpoint 崩溃重放提交，command_id/idempotency_key 也必须稳定。

Root checkpoint 只存目标/context refs、plan_id/version、当前 round、wait 身份、review decision/命令键等恢复所需数据。ReviewContext 从业务库按轮次装配，不依赖内存或仅看当前轮摘要。

ReviewContext 至少包含原目标、用户约束、有效 DAG、全部可复用上游结果摘要、当轮成功/失败/不确定性、待执行任务及阻塞原因、剩余预算。Root 输出严格的 CONTINUE/REPLAN/FINISH tagged union；非法结构有限修复，命令状态冲突时重新读事实，不用盲重试掩盖版本变化。

恢复握手：

1. Root 进入 Wait 节点，调用 interrupt，当前调用退出。
2. Runtime 读取 checkpoint 与 pending interrupt，记录 wait_generation、round_id、interrupt_id、checkpoint_id 和 armed 状态；释放执行锁。
3. RoundCompleted 先到则 Inbox/Resume Job 保持等待就绪，延后重试；不丢弃，也不盲目 resume。
4. Dispatcher 领取 Root Job，取得 thread 独占权，重查 Run/Plan、事件轮次、wait generation 和实际中断，调用 `Command(resume=...)`。
5. Root 提交的审查命令以 round 为业务幂等身份；图已前进但 Job 未确认时，重投检查命令回执/checkpoint 再收敛，不再次推进下一轮。

“恢复一次”指每轮只产生一次有效业务决策；崩溃窗口内模型可能重算，不能承诺 LLM 天然只调用一次。提交的审查决定、版本及下一轮效果必须至多一次。

PlanFailed 走独立故障处理节点：加载错误事实后按可恢复策略处理或生成失败答复，不当成成功 Round；PlanCancelled 只同步 Run/SSE，不唤醒 Root 模型。决定校验失败可以重新生成候选命令，但必须仍绑定同一待审 round；一个 round 最多提交一次成功决策。

Revision 校验保留集合、取消集合、新增集合无冲突；完成任务不改内容；依赖只能指向新版本中有效 Task；引用的旧结果必须匹配定义和输入语义；完整新 DAG 无环。旧 Round、版本和结果永久保留审计关联。

FinishPlan 与最终回答生成之间另有故障窗口：先记录 finishing/finalization 工作身份，再幂等持久最终消息、Run 终态和记忆处理 Job。Plan 已结束不等于回答已发出；重启恢复 Finalize，禁止重派 Task 或重复写最终消息。最终汇总失败用独立重试/错误状态表达。

## 8. Worker Agent Loop、工具和审批

Worker Graph：Initialize → Think → PrepareToolCall → Policy → Approval interrupt（如需）→ ExecutionGate → Execute → Observe → Think；达到完成条件后输出 TaskCompleted，不可恢复错误或预算耗尽输出 TaskFailed。

- 不把完整 DAG、其他 Worker 对话和全部会话工具注入 Worker；只给任务、系统约束、所需上游 refs、有效工具集合。
- 系统根据工具注册、全局启用状态和 Worker 能力提供可用目录；Root 从目录中为任务选择 allowed_tools，Worker 在该范围内自主选择具体调用。用户表达目标和业务限制，无需指定工具名或白名单；具体操作仍按审批策略执行。非法模型输出在入口拒绝并让模型有界修复，不能通过清空 allowed_tools 掩盖不合法计划。
- 模型真正绑定可调用工具 schema；Built-in/MCP 经过同一执行网关，不能绕过审批或取消检查。
- 保留各轮 messages/tool results/预算；普通工具错误返回模型观察，重试和不可恢复失败有明确分类。
- 取消令牌在模型与工具边界检查；已知异步 Provider 保留异步执行，同步调用只在适配器中隔离。

审批具体链路：Prepare 持久化不可变 tool_call_id/operation_key/args_hash → 专用 Approval Node interrupt → Runtime 确认实际 checkpoint/pending interrupt → Collector 幂等写审批、Task=waiting_approval 与 Outbox → 释放执行调用和活跃额度。

决策 API 在一个事务中核对操作者、归属、期限、Plan/Run、execution/interrupt 和参数摘要；PENDING 条件更新到 APPROVED/REJECTED，写审计与唯一 ResumeWorker Job，202 返回。客户端不用提交可信的 task_id/run_id，由服务端读取审批归属。

Worker Resume Dispatcher 独占 thread，确认同一 interrupt，以 interrupt_id → decision 映射恢复。不同 Worker 的审批互不影响；同 Worker 多中断分别绑定具体操作，不按数组顺序猜测。

拒绝/过期形成显式 ToolDenied，回到 Observe/Think；不是自动 TaskFailed，更不会唤醒 Root。通过后仍需副作用前再次验证状态、取消 epoch、租约、参数哈希和授权未消费。

工具幂等账本不能只使用“新 execution_id + 新序号”作为跨重试防重方案：相同已准备操作在 checkpoint 恢复和确认重试中复用 operation_key。完整重做一个失败 Task 时，先核对既有操作及外部结果；不确定状态禁止无条件重发。支持外部幂等键的工具透传同键；无幂等能力的工具在可能已发送后标 UNKNOWN/待对账，不承诺恰好一次。

数据库中的操作门禁与外部 API 发送不是一个事务：取消先提交则拦截，操作已获准并开始发送则属于在途副作用，可能无法撤回。不能用本地行锁或 fencing 宣称消除了这个边界。

## 9. 用户停止、租约与故障修复

停止 HTTP 直接调用 RunCancellationService，不把首次 CANCELLING 落库排在普通 Root 命令之后。鉴权和幂等后锁 Run/Plan，原子置取消中、增加 epoch、取消未启动 Task/待审批/未消费恢复 Job、写控制 Outbox，再返回 202。

- 无 Plan：阻断后续 SubmitPlan，取消 Root 当前调用。
- 有活动 Plan：阻断派发、Continue/Revise/Resume，向 Worker 控制通道发 CancelExecution。
- Plan 已 FINISH、Root 正生成最终答案：Plan 终态保持，取消尚未终结的 Run/Root Finalize；不能因 Plan 已完成漏掉停止。
- Run 已完成/失败/取消：返回原终态及 already_terminal=true，不改历史。
- 所有执行退出确认或被可靠隔离后才提交 CANCELLED；外部动作未知独立保留 UNKNOWN。

后台控制消息、心跳和 Job 使用数据库持久状态；内存 registry 只用于定位本进程调用并发出及时取消，不承担权威事实。取消期间 Round 不触发新 Review，旧 Root 事件归档/作废，不能让 Run 复活。

一个 thread 同时仅一个图调用：执行租约 + thread 独占锁 + 心跳失效主动终止调用。注意只在业务结果上 fencing 不足以阻止旧进程继续写 checkpoint；同 thread 恢复必须确认旧 owner 已退出/失去可靠执行权。若无法确认，不并发恢复同一 thread；可隔离旧 attempt 后以新 thread 重试，并先核对副作用账本。不要仅凭 lease 时间到就假设旧进程已停止。

Reconciler 低频、索引化、分批处理：

| 崩溃/异常窗口 | 修复动作 |
|---|---|
| Outbox 或执行 Job 长期未确认 | 到期重投；consumer/命令键去重 |
| Worker claim 后进程退出 | 核验租约、thread/checkpoint/账本；同执行恢复或新 attempt 重试 |
| checkpoint 已中断，审批未创建 | 从有效 execution 的 pending interrupt 补建审批 |
| 审批决定已记录，Worker 未恢复 | 重新领取持久恢复 Job |
| Round 全终态，屏障事件缺失 | 重算核验并按唯一键补写事件；正常路径仍由事件事务推进 |
| Root 未 armed，事件已收到 | 核验 checkpoint 后补 armed，再投递保留的 Job |
| Root 决策提交后进程退出 | 查命令回执/checkpoint 收敛，不再创建新 Round |
| CANCELLING 跨重启遗留 | 重发控制消息、收敛执行、记录外部 UNKNOWN，不提前宣称停止 |
| Finalize 写消息前后崩溃 | 按 run/finalization 唯一键补全消息、Run 状态及记忆 Job |

## 10. HTTP、SSE 与前端

| 接口 | 新行为 |
|---|---|
| `POST /api/sessions/{session_id}/runs` | 保持异步提交和 command_id 幂等，进入新 Root 入口 |
| `POST /api/runs/{run_id}/cancel` | 接受 Idempotency-Key；返回 run_id、CANCELLING/原终态、already_terminal；不等待 Worker 全部退出 |
| `POST /api/approvals/{approval_id}/decision` | 校验 APPROVE/REJECT 决定；202 仅表示决定已记录和恢复作业已创建 |
| `GET /api/runs/{run_id}/orchestration`（拟新增） | 当前 Plan/version/round、冻结成员、Task/Execution、审批和取消事实的查询快照 |
| `GET /api/plans/{plan_id}/versions/{version}`（拟新增） | 不可变 DAG 与修订原因，用于版本历史展示 |
| 现有会话 SSE / 历史事件查询 | 增加 Round/Review/Version/取消事件，沿用序号重放和服务端 occurred_at |

审批接口上线时同步替换旧 resolve/batch-resume 语义；若 UI 保留批量操作外观，也必须对每个审批分别授权和建恢复 Job。批次只是展示分组，不是 Worker 共享放行凭证。

SSE 增加 plan_version/round_id/execution_id 和通用追踪身份；关键事件经 Outbox 投影进入 agent_events。投影以源 event_id 去重，再分配 session_seq，不能每次重投生成新的前端事件。实时通知失败由历史重放恢复，既有 persisted occurred_at 不被接收时间覆盖。

前端范围：

- `types/index.ts`、`types/events.ts`、`api/client.ts`：统一新状态与响应。
- `store/chatStore.ts`、`hooks/useSessionEventStream.ts`：幂等投影、刷新后快照与事件重放衔接。
- `WorkerTaskCard`、`OrchestrationSummary`：展示 Round 成员、当前排队/运行/审批/重试及审查结果。
- `ApprovalDialog`、`ApprovalsView`：多个独立审批；已决定/已过期/已取消不可重复操作。
- `Chat`：停止后先显示“正在停止”，收到终态显示“已停止”；未知外部结果另列。
- `RunChainView`：关联 Plan Version → Round → Task → Execution → Tool/Approval；Review 和 Revision 有可读原因。已有演示数据必须与真实历史清楚区分。

Athena 按单用户产品实施，不增加多租户隔离或 `user_id` actor 映射。全局登录保护用于访问控制；Run 快照按 `run_id` 读取其 Plan 和审批事实，Plan Version 按 `(plan_id, version)` 读取，命令仍需校验 Run、Plan、Task、审批之间的业务身份关系，避免错误关联。

## 11. 分阶段实施与完成条件

| 阶段 | 主要交付 | 完成条件 |
|---|---|---|
| P0 契约与基线 | 当前脏树快照清单、状态/命令/事件/Review 类型、验收矩阵、预算配置 | 明确替换清单和默认口径；记录现有测试结果与已有失败 |
| P1 数据与事务内核 | Version/Round/Execution、共享 UoW、五类命令处理、Outbox/Inbox、取消 epoch 基础 | 真实 PG 下冻结集合、命令去重、版本 CAS、并发终态屏障成立 |
| P2 独立 Worker Runtime | 持久 execute Job、claim/lease/fence、通用 Graph、Agent Loop、工具读路径和结果引用 | 两 Worker 并行；重启不丢 Job；旧结果拒绝；本轮重试不提前完成 Round |
| P3 Root Review 与 Revision | 真 interrupt/resume、Inbox 就绪握手、三态决策、版本修订、幂等 Finalize | 不轮询、不常驻等待；早到事件可靠；Review 后才启动下一轮 |
| P4 工具审批与停止闭环 | Policy、操作账本、真实审批中断、决定/恢复 Job、完整取消控制 | 只暂停对应 Worker；拒绝可继续；审批/停止竞态无新授权；停止收敛 |
| P5 前端与 API 切换 | 新查询/decision/cancel、状态与事件投影、轮次/版本视图 | 浏览器可操作多审批、观察轮次和停止；断线刷新可恢复 |
| P6 故障验收与旧链删除 | Reconciler、故障注入、双实例并发、真实环境验收、文档/旧代码清理 | 新执行路径唯一；必要验收全部有证据；未覆盖能力明确记录 |

P1 就建立取消状态、Outbox、幂等身份和共享事务；P2 就建立 lease/fence，P4 就实现副作用账本。P6 是验证和补全修复路径，不是第一次补这些基础能力。

建议按上述阶段拆 7 个可审查提交/PR，每一阶段包含自己的测试。开发期间新增模块可以暂不装配；生产入口最终一次切换到新语义。不要让旧自动补派 Scheduler 和新 Round Scheduler 同时处理同一 Run。

## 12. 验收矩阵与验证层级

| 编号 | 场景 | 必须断言 |
|---|---|---|
| A01 | 同轮 A/B，A 先完成，C 依赖 A | B 未终结前无 Review、C 不启动；B 终结仅一个 RoundCompleted |
| A02 | 本轮 10 ready、并发 3 | Round 固定 10 成员；全程不超额度；不加入下一层任务 |
| A03 | B 等审批，A 完成 | 无活跃等待协程；Root 不恢复；审批后 B 原 thread 继续 |
| A04 | 两 Worker 同时审批 | 独立决定/恢复，批准一个不解锁另一个 |
| A05 | REJECT/EXPIRE | ToolDenied 进入 Worker 观察；无未经许可调用；不强制全局 Replan |
| A06 | RoundCompleted 早于 Root interrupt | Inbox 留存；Root 就绪后仅一次有效审查决策 |
| A07 | Continue 重复 10 次、同键不同内容 | 仅一下一轮；不同内容明确冲突 |
| A08 | Replan 复用完成结果、取消剩余、增加新任务 | 历史版本/轮次不变，新 DAG 正确生效，变更定义不能复用旧 Task ID |
| A09 | 单 attempt 失败后重试 | Task 不提前终态、Round 不提前完成；重启后重试次数不清零 |
| A10 | 无 ready 的失败依赖；非法/循环 DAG | 返回明确错误并进入有界审查处理，不永久挂起 |
| A11 | 批准后、Resume 前宕机 | 决定和 Job 同存；重启后恢复；无重复副作用 |
| A12 | claim/工具发送/结果写入/checkpoint 前后宕机 | checkpoint/账本/外部幂等共同判定；未知结果显式对账，不盲重发 |
| A13 | Stop 与审批、Continue、Revise、完成事件并发 | 按事务顺序裁决；取消后不复活、不再派新轮次 |
| A14 | 规划中/审批中/并行中/Review 中/Finalize 中停止 | 及时进入 CANCELLING；不调用取消后 Root Review；终结时机正确 |
| A15 | 已终态后停止、重复停止 | 保留原终态，同一取消进度，不重复语义事件 |
| A16 | lease 过期但旧 Worker 晚到 | 旧结果不能推进状态；同 thread 不出现双 checkpoint writer |
| A17 | 外部 API 已发出无法取消 | 内部可收敛，保留 UNKNOWN/审计，UI 不宣称外部已回滚 |
| A18 | SSE 断线/刷新/重复投递 | 后台照常；关键事件重放、审批和版本快照一致；时间不漂移 |
| A19 | 最终消息/记忆任务写入前后崩溃 | 最终消息与记忆 Job 各至多一份；Run 可恢复收敛 |
| A20 | 未授权用户、跨 Run 审批/结果引用 | 资源权限拒绝；不改变状态，不执行工具 |

验证分层：

1. **纯领域测试**：DAG、状态机、冻结成员、Revision、ReviewDecision 校验。
2. **应用用例测试**：命令回执、事件重复/乱序、重试、审批拒绝、取消路由；替身测试只证明用例逻辑。
3. **真实 PostgreSQL 集成**：独立连接/两个消费者竞争、唯一约束、CAS、锁顺序、事务 rollback、跨仓储原子性、Outbox、租约。
4. **LangGraph 集成**：项目锁定版本 + AsyncPostgresSaver，确认真的产生 pending interrupt；重建 runtime 后用同 thread Command 恢复；检查中断前节点重放。
5. **真实 Provider/工具**：真实模型生成多轮 Plan/Review；只读与需审批工具各一条；Docker shell 通过实际沙箱执行，不能回退宿主 shell 来冒充验收。
6. **浏览器 E2E**：真实聊天入口，观察并行、审批、Review、Replan、停止、刷新重放；保存截图/事件/业务行/执行身份的对应证据。

延时等待测试使用可控时间验证 3 小时审批语义，不要求测试真实空等 3 小时。故障注入通过明确 barrier 控制“提交后/ack 前”等窗口，不靠概率性 sleep。

测试安排：扩展 `tests/unit/test_orchestration_domain.py`，替换 Scheduler/Root Graph/Worker 测试中的旧语义；在 `tests/integration/` 新增 round、delivery、approval、cancellation、graph_recovery 套件；保留层级边界测试。前端执行 `npm run typecheck` 和 `npm run build`，视觉/交互结论必须另有浏览器证据。

后端测试从 `backend` 目录，使用项目根 `.venv/bin/python`（即 `../.venv/bin/python`）并设置 `PYTHONPATH=src:.`；PG 集成测试显式要求隔离测试库配置，跳过不计通过。不要使用生产库做故障注入。

## 14. 第一批实际编码任务

第一批只实施 P0/P1，形成能够用真实 PostgreSQL 验证的确定性内核：

1. 新增 Round、PlanVersion、Execution、命令/事件、Review 类型；固定统一状态值和 schema_version。
2. 建立共享事务端口，在 ORM 模型中定义索引，并修正 cancelling 的活跃 Run 约束。
3. 实现 Submit/Continue/Revise/Finish/Cancel 命令、固定 Round 与唯一命令回执。
4. 实现 WorkerEvent ingestion、attempt 与 Task 分离、轮次终态屏障和 Outbox/Inbox。
5. 通过 A01/A02/A07/A08/A09/A10/A13 的数据库协议测试，再接入 Worker 和 Root 图。

首个里程碑：**即便没有真实 LLM，仍能在 PostgreSQL 中证明轮次、版本、重试、取消和投递协议正确；后续图和 Provider 是协议的调用者。** 这只是内核验收，不等于整个 Agent 已重构完成。

## 15. LangGraph 版本核对与本轮验证范围

本轮只读核对发现：`backend/pyproject.toml` 锁定 langgraph 1.2.11；项目 `.venv` 实际为 langgraph 1.2.11、langgraph-checkpoint-postgres 3.2.0。该环境 `interrupt` 签名只有 value，Command 接受 resume 参数。

官方文档说明恢复需使用同一 thread_id 和 Command(resume)，中断节点恢复时会从节点开头重跑。因此命令提交、准备操作、审批中断和工具执行必须拆节点并配合幂等。最新文档中的 interrupt response_schema 要求 langgraph >=1.2.12，本次不依赖它；需要类型校验就在应用入口显式完成。

参考：[LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。这些文档只支撑图恢复方式；Round、事务投递、审批授权和取消状态均需 Athena 自己实现。

本节记录 2026-10-21 编写实施方案时的核对范围，不再代表当前实现和验证状态。后续实施证据见第 16 节。

## 16. 当前实施进度与验收缺口

截至 2026-10-22，P1–P6 的目标执行链已落地并完成切换：Root Review、版本化 Round、持久 Worker Job、审批恢复、取消与超时控制、工具副作用账本、查询 API、前端 Run Chain 和旧调度路径清理均有实现证据。A01–A20 矩阵现均有专门测试或真实浏览器验收；A17 使用确定性 API stub 验证结果未知时不盲目重发，未做第三方服务的实际联调。

P5 已切换审批决定路径：新增 `POST /api/approvals/{approval_id}/decision`，返回 202；待审批查询补充 `run_id`、`plan_id` 和 `task_id`；前端保留批次分组展示，但对每条审批分别提交决定。新增 `GET /api/runs/{run_id}/orchestration`，从 PostgreSQL 汇总 Run 取消事实、Plan Version 摘要、固定 Round 成员、每个 Task 的 Execution 尝试和审批决定；新增 `GET /api/plans/{plan_id}/versions/{version}` 返回不可变 DAG 与修订原因。Run Chain 现在在加载历史运行时同时读取事件时间线和持久化快照，执行树可沿 Version → Round → Task → Execution → Approval 恢复，选择 Version 可查看对应 DAG。查询接口契约测试、PostgreSQL 快照集成测试和前端 TypeScript 检查通过。本轮用临时 PostgreSQL 记录完成真实本地浏览器验收：同批次两条审批各自提供允许/拒绝选择，选择第一条时提交仍禁用、第二条选择后提交启用；Run Chain 展示两个 Plan Version、已完成和进行中的 Round、对应 Task/Execution 与两条待审批节点；刷新后这些持久状态恢复，重新选择 Version 2 可再次读取其 DAG。验收记录随后从测试库删除，没有提交审批决定。当前共享登录认证符合 Athena 的单用户产品定位，不要求将登录用户映射到 `agent_runs.user_id`；查询与命令通过持久化身份关联校验 Run、Plan、Version、Task 和审批关系。

P6 早期已有 PostgreSQL 验收：一条模拟 Worker 崩溃留下的过期租约，由两个独立数据库会话同时领取，验证只产生一个新租约、fencing token 从 7 增至 8、执行代际保持不变、Job 投递次数增加，且旧持有者无法续租；一条在 PostgreSQL checkpointer 中持久化 Worker 审批 interrupt，关闭并重建 checkpointer 和 executor 后找回原 interrupt，获批后仅执行一次工具并完成原任务；一条从 PostgreSQL 直接读取 Run 快照和不可变版本，核对取消状态、Round 成员、Execution fence、审批决定及 DAG；一条从活跃 Worker 和待审批状态开始取消 Run，验证重复取消不重复写 Outbox，过期租约回收后 Run/Plan/Round/Task/Execution/Job 收敛到 cancelled，旧 lease 不能确认退出，终态重复取消不追加事件，同时保留 `external_effect_status=unknown`；一条模拟 SSE 事件已持久化、Dispatcher 在确认 Outbox 前崩溃，验证租约过期后新 Dispatcher 可重投，事件 transition ID 去重且 Outbox 最终确认发布；一条模拟 Worker 在工具操作账本写入 executing 后崩溃，下一执行代际面对未知结果时拒绝重发工具调用，同时保留已完成操作跨代际复用结果的验证。上述验收均在随机临时 schema 内运行并于结束时删除。当前测试数据库结构与 ORM 模型一致，后端重启后同一审批接口由 500 恢复为 200。浏览器端现已覆盖多审批选择、版本 DAG、Round/Execution 投影及刷新恢复。

P6 的真实 Provider 闭环现已增加一条浏览器验收：真实聊天请求生成包含两个并行 Task 和一个合并 Task 的 Plan；Root 将 Round 1 挂起到 PostgreSQL checkpoint，两个 Worker 完成后恢复 Root、执行 Review 并启动合并 Round；最终 Run、Plan、两轮 Round 和三个 Task 全部完成，浏览器显示 3/3 并收到可读中文总结。Run ID 为 `2026.10.22.02.09.19.200`。另一次浏览器验收在 Run 执行中点击停止，Run 收敛至 `cancelled`、`cancellation_epoch=1`；刷新页面后仍恢复原请求和“任务已取消”事件。该次停止发生在 Plan 创建前，不替代活跃 Worker 的取消验证。验收中发现并修复了 Root 将 LangGraph interrupt 当成最终结果、同一节点循环挂起多轮导致 resume 身份错配、Review 响应遗漏必填 reason，以及默认结果投影把 Worker 内部 envelope 渲染为 Python 字典的问题；计划解析也兼容对象边和二元组边，初始 Plan/Version 写入顺序通过显式 flush 满足外键约束。对应的受影响单元测试与全部 11 条 PostgreSQL 集成测试合计 48 条通过。用于验收的 12 个合成会话已软删除，用户原有会话未改动。

本轮继续补齐三处实现和验收覆盖：Replan 在创建下一轮前显式 flush 新的不可变 PlanVersion，修复 PostgreSQL 复合外键的实际写入顺序；PostgreSQL 集成路径现在覆盖提交 v1、保留完成结果、取消未执行任务并提交 v2，以及为新 Round 创建 Worker Job；Docker sandbox 将 pid 限制接入启动配置，并支持无工作区挂载、只读根文件系统。前端取消事件会把运行中或等待中的时间线节点收敛为 `cancelled`，同一 Round 的完成事件更新其开始节点。测试库已启动并用于真实 PostgreSQL 集成验证；本轮 `test_target_integrations.py` 与 `test_postgres_plan_submission.py` 合计 9 条通过。前端 TypeScript 检查与构建通过，构建保留既有 chunk size 提示。

P6 本轮新增真实 Provider、浏览器、PostgreSQL、正常审批与 Docker 沙箱闭环，Run ID 为 `2026.10.22.03.39.44.737`。真实聊天创建并完成单 Worker Plan；持久化 Task 的唯一权限为 `allowed_tools=["exec_shell"]`，用户通过 Athena 审批界面批准唯一命令 `python -c 'import sys; print(sys.version.split()[0])'`，Worker 在无网络、只读根文件系统、不挂载工作区且有限制内存/CPU/PID 的容器内执行一次，结果为 `3.12.14`。数据库核对确认 Run、Plan、Task 与 Approval 均已终结，且恰有一条成功 ToolOperation，未出现重复副作用。

另完成真实 Provider 的活跃 Worker 取消浏览器验收，Run ID 为 `2026.10.22.03.47.06.651`。单 Worker Plan 已进入运行态后从聊天界面点击停止；PostgreSQL 中 Run、Plan、Round、Task、Execution 均收敛为 `cancelled`，`cancellation_epoch=1`，Worker Job 已清空；刷新前的浏览器时间线同时显示“执行计划已取消”“任务已取消”和“本轮任务已取消”。合成会话已软删除，Run 与取消事实保留用于审计。

为使该真实闭环成立，本轮修复 Worker Job 从 PostgreSQL 加载 allowlist 时丢弃 JSON 列表、用户显式工具白名单未贯穿任务理解与 Plan 物化、Provider 的 Qwen 风格文本工具调用未解码、审批恢复仍使用旧 fencing token 等问题。Root 现在持久化完整原始用户目标，提示模型遵守明确的 Plan Version/阶段边界，并将空任务列表作为无效计划进行一次有界格式修复。43 条定向单元测试及全部 11 条 PostgreSQL 集成测试通过。更广的单测运行有 133 条通过、5 条失败，涉及认证路由期望、旧取消状态契约、审批事件替身缺少 `publish`、旧 SSE 路由访问方式；全量收集还遇到引用不存在 `infrastructure.observability` 的旧测试，以及 unit/integration 同名模块冲突，因此当前不能宣称全后端单测通过。清理已完成：移除临时 `exec_shell` 配置、软删除合成测试会话，保留 Approval/ToolOperation 审计行；未发现待处理审批、未确认 Outbox 或验收 Run 的活动 Worker Job。

上一轮真实 Provider Replan 曾把“独立审查”提前放进 Version 1，因此没有创建 Version 2。随后加入应用层版本约束：明确分版的计划必须给每个任务标注连续 `plan_version`，Root 只提交 V1，并在当版所有任务完成且经过 Root Review 后，以保留现有 DAG 的幂等 Revision 提交下一版；跨版和同版新任务依赖都会映射到 Run 级 Task ID，未知依赖拒绝提交。

最新真实浏览器验收 Run ID 为 `2026.10.22.04.18.58.408`。V1 恰有一个 `initial_draft` 并完成；Root Review 后 PostgreSQL 中 Version 2 保留原任务，只新增依赖它的 `independent_review`；第三次 Worker 尝试完成审查，最终 Run、Plan、两轮 Round 和两个 Task 全部完成。前两次 Provider 调用超时，第三次成功。执行期间服务器热重载暴露出 `MAX(session_seq)+1` 的并发竞态：业务终态已经提交，但 `run.completed` 与同序号事件冲突，刷新页面仍显示运行中。事件序号现通过 PostgreSQL advisory transaction lock 按会话串行分配，并新增 24 个并发 Publisher 的 PostgreSQL 集成测试。为让本次合成验收记录可从页面重放，测试库中补记了唯一缺失的 `run.completed` 事件；刷新后浏览器显示对话完成和 2/2 Worker 完成。此补记只作用于该合成验收记录。

本轮还新增 Worker 终态事务提交后丢失回执的 PostgreSQL 故障注入：重放同一事件只返回既有回执，Task/Execution/Job/Round、Inbox、Outbox 和 Root Resume 均未重复推进。生产 Root 现要求通过 `OrchestrationCommandHandler` 原子提交计划；旧进程内 `WorkerSchedulerService`、直接调度后备路径及无操作的 `WorkerExecutor.cancel()` 已删除。

在用户授权的本地单用户测试会话中，另以真实浏览器和 PostgreSQL 完成一次高风险工具审批闭环，Run ID 为 `2026.10.22.04.47.54.126`：只建立一个 Worker task，明确 allowlist 仅为 `exec_shell`；Athena 先生成待审批卡片，用户在界面批准后由 Worker 在 Docker `--network none`、无工作区挂载、只读根文件系统的容器中执行唯一命令 `python -c 'import sys; print(sys.version.split()[0])'`，退出码 0、输出 `3.12.14`，ToolOperation 只成功一次。用于本次验收的临时工具行已删除，测试会话已通过 Athena 界面软删除；失败的前置尝试未执行命令。该记录验证工具审批和隔离执行路径，不替代其余 A01–A20 矩阵覆盖。

本轮将 pytest 默认导入模式设为 `importlib`，避免 unit/integration 中同名测试模块冲突。以项目本地 PostgreSQL URL 在当前测试进程中设置 `ATHENA_TEST_DATABASE_URL` 后，全量后端与 PostgreSQL 测试为 `368 passed, 2 warnings`；`compileall` 与 `git diff --check` 通过。警告为既有的 FastAPI duplicate operation ID。PostgreSQL 验收覆盖 A01 Round 屏障与重投去重、A02 双 engine 下的并行上限与固定 DAG 成员、A04 每个 Worker 独立审批恢复，以及 A06 终态 Inbox/Resume Job 原子写入和 checkpoint 就绪前延迟。新增 A03 在 A 完成、B 等待审批时验证没有提前创建 Root Resume Job；B 获批后由新 lease 接手同一 execution/thread，只有 B 终结才创建唯一 Root Resume，配合 LangGraph interrupt 单测覆盖挂起而非轮询等待。新增 A05 用真实 PostgreSQL checkpointer 重启恢复 approved/denied/expired 三种审批结果，拒绝与过期均由 Worker 观察到明确 ToolDenied 且不执行工具；A07 并发 Continue 幂等与冲突检查；A09 通过 app engine 重建、lease 接管、迟到结果拒绝及失败后重试验证 attempt 计数与 Round 状态持久化；A10 将失败依赖下的无 ready task 明确返回 `BLOCKED_DEPENDENCIES`，Root 最多复审一次，仍无可运行任务时以 FINISH failed 收敛，Finish 同时取消剩余 pending task；A11 新增审批已提交后重建 app engine 并恢复同一 execution/thread 的 Worker Job 验收，与现有 PostgreSQL checkpointer 恢复及获批工具单次执行验收互补；A13 覆盖 Stop 与 Worker 审批、终态事件、Continue 和 Revise 的并发裁决，Stop 后没有活动审批或可领取 Worker Job；A15 在 PG 验证 completed/failed Run 的重复 Stop 保留终态和取消元数据，并确保不产生额外 Outbox 事件。A19 在 Run 完成事务中原子写入最终回答 Outbox、RunCompleted Outbox、唯一 memory job 和命令终态，并通过丢失完成回执、Outbox 重放及 Memory Worker 崩溃恢复验证可收敛。Ruff 未安装在项目虚拟环境中，未执行 lint。Athena 按用户确认以单用户产品为目标，不要求多用户身份映射。

新增 A11 联合故障注入用例 `test_approved_worker_effect_resumes_once_after_full_app_restart`：真实 PostgreSQL 先保存 Worker checkpoint interrupt 和审批请求，再原子提交批准及 Resume Job；模拟 app 重启后由新 Worker Job repository 领取同一 execution/thread，使用新 checkpointer 找回 interrupt、从 PostgreSQL 恢复决定，并通过持久 ToolOperation ledger 恰执行一次获批工具，最终 Task、Execution、Job 与 Operation 全部终结。

A12 在该联合测试中增加账本提交后、Worker checkpoint 更新前的进程取消：确认外部工具只执行一次、ToolOperation 已完成、Resume Job 尚运行且 checkpoint interrupt 仍待处理；过期 lease 被第二次重启接管后从账本重放结果并提交 Task 终态。该复现暴露 LangGraph 同一 superstep 同时写入 Worker `execution` 的冲突，Resume 现在把新 lease 快照作为 interrupt 决策的一部分，由审批完成节点合并并写入 checkpoint。新增 `test_root_resume_recovers_checkpoint_written_before_run_arm`：真实 PostgreSQL checkpointer 先保存 Root interrupt，再重启 checkpointer；模拟进程未将 interrupt/checkpoint ID 写入 Run 行即退出，新的 Root Resume dispatcher 从 checkpoint 找回身份、补登记并恢复同一 Root 图。

补齐 A12 的 Root Review 提交窗口：`test_root_review_reuses_decision_after_commit_before_checkpoint` 在 PostgreSQL 事务提交 `FINISH` 决议后、Root 节点返回供 LangGraph checkpoint 前注入进程中断；随后以 checkpoint 前的同一 interrupt 输入重放应用节点，工作流先从持久命令恢复决议，不再调用 Root Review，并由幂等命令只保留一条 `PlanCompleted` Outbox。该用例在应用节点边界做确定性故障注入，与 Worker 工具账本故障注入及 Root checkpoint 登记恢复共同覆盖 A12 的结果提交/恢复边界。

补齐 A18 的断线恢复证据：在临时本地浏览器会话中先建立 EventSource，再导航离开使其断开；断线期间向 PostgreSQL 持久化 `run.started`、消息开始/分块/完成及 `run.completed` 六个有序事件，重新打开 Athena 后界面重放出完成消息和原事件时间。临时会话随后通过 `PostgresSessionRepository` 软删除。`test_sse_replays_from_last_event_id_and_cleans_up` 现在也将相同 session sequence 同时放入 replay 与实时队列，确认浏览器流只输出一次，并继续发送下一序号。此前浏览器验收已经验证审批列表、Plan Version/DAG、Round/Execution 快照刷新恢复；三者共同覆盖后台持续推进、断线重放、重复投递去重、持久快照一致和事件时间稳定。

A16 为 Root 与 Worker Graph invocation 接入同一 PostgreSQL advisory session lock，锁键使用稳定 thread ID 并覆盖完整图调用；持锁的数据库连接异常关闭时 PostgreSQL 会释放锁。`test_postgres_worker_thread_lock_serializes_separate_app_instances` 以两个独立连接验证同 thread 互斥，Worker dispatcher 单测验证 Graph 只在锁内运行；现有过期 lease/迟到事件验收继续证明 fencing 拒绝旧结果。

A17 新增 `test_external_effect_survives_cancel_as_unknown_and_is_audited`：模拟远端已接收外部请求、客户端在收到回执前被取消，随后 Stop 将 Run 收敛到 cancelled 并保留 `unknown` 结果和仍处于 executing 的 ToolOperation 审计记录；重放被拒绝，外部调用次数保持一次。Run Chain 快照投影明确显示外部效果未知且 Stop 不保证撤回已发送操作。前端 `typecheck` 与 production build 通过；构建仍提示既有的大 bundle 体积警告。此验收使用确定性 API stub，不代表真实第三方服务联调。

A20 按单用户产品决策跳过用户身份隔离，新增 `test_cross_run_approval_reference_is_rejected_without_side_effects`：PostgreSQL 拒绝另一 Run 或冒用 Task 的审批请求且不改变 pending 记录、不产生 Outbox；正确 Run/Task 仍可批准。`test_worker_result_reference_from_another_run_is_not_read` 确认没有登记在当前 Worker execution state 的结果引用会被拒绝，且不会读取底层存储。

### A01–A20 覆盖盘点（2026-10-22）

状态口径：**已覆盖**表示当前有与矩阵触发条件相符的专门测试/验收；**部分覆盖**表示只验证了其中的协议或相邻路径；**缺口**表示还没有足以支撑该条结论的专门证据。单用户产品不验证“其他用户”身份隔离，但仍需验证跨 Run 的业务身份关联。

| 编号 | 状态 | 当前证据与剩余工作 |
|---|---|---|
| A01 | 已覆盖 | `test_round_barrier_holds_dependent_task_and_resumes_root_once` 在 PG 中先完成 A，确认 B 未终结时 Round/Plan 仍运行且依赖 C 没有 RoundTask/Job；B 终结后只生成一个 RoundCompleted/Root Resume Job，重复投递 B 被 Inbox 去重。 |
| A02 | 已覆盖 | `test_two_app_instances_respect_plan_parallelism_with_ten_ready_tasks` 用两个独立 engine 竞争同一 Plan，首轮固定 10 个成员且最多 3 个 Execution/Job 获租；依赖下一层的第 11 个 Task 保持 pending，不会混入本轮。 |
| A03 | 已覆盖 | `test_root_resume_waits_for_approved_worker_to_finish_same_round` 在 PostgreSQL 验证 A 先完成、B 等审批时无 Root Resume Job；批准后新 Worker lease 继续同一 execution/thread，B 终结后只创建一个 Root Resume Job。`test_root_wait_node_interrupts_and_rechecks_after_resume` 与 `test_root_resume_dispatcher_retries_until_matching_interrupt_exists` 验证 Root 以 checkpoint interrupt 挂起并在匹配中断就绪后恢复。 |
| A04 | 已覆盖 | `test_resolving_one_worker_approval_only_unblocks_its_execution` 在 PG 中让两个 Worker 同时进入 waiting_approval；批准 A 只产生并可领取 A 的恢复 Job，B 仍 pending 且没有可领取 Job；拒绝 B 后才恢复 B 自己的 execution。 |
| A05 | 已覆盖 | `test_worker_approval_resumes_after_postgres_checkpointer_restart` 以真实 PostgreSQL checkpointer 重建验证 approved/denied/expired 决议；denied/expired 两支的 ToolDenied 被重启后的 Worker 观察到，工具执行记录为空。审批过期仓储测试另验证 EXPIRED 决议与 Resume Job 同事务写入。 |
| A06 | 已覆盖 | `test_round_barrier_holds_dependent_task_and_resumes_root_once` 验证终态 Inbox 与 RoundCompleted/Root Resume Job 在 PostgreSQL 事务内一并持久化，任务先于 interrupt 就绪时保留并延迟，checkpoint arm 后 dispatcher 仅 resume 一次；`test_two_app_instances_claim_one_root_resume_and_wait_for_interrupt` 另验证两个 app engine 竞争只有一个 Root Resume Job 获租。 |
| A07 | 已覆盖 | `test_round_barrier_holds_dependent_task_and_resumes_root_once` 在 PG 中并发提交 10 次同内容 Continue，仅一个回执非 deduplicated 且只创建一个下一 Round/Job；复用相同 key 改变 Review 内容会明确冲突。 |
| A08 | 已覆盖 | `test_submit_and_replan_preserve_completed_results_in_postgres` 验证保留完成结果、取消旧任务、写入新版本与 Round/Job。 |
| A09 | 已覆盖 | `test_worker_restart_reclaims_failed_attempt_without_closing_round` 重建 app engine 后从 PostgreSQL 接管过期 attempt，拒绝旧 Worker 迟到结果；新 Worker 的可重试失败原子创建 attempt 2，Task/Round 继续 pending/running，终态计数不增加且重启后可领取新 Job。 |
| A10 | 已覆盖 | `test_blocked_continue_returns_explicit_error_then_finish_cancels_pending_tasks` 验证失败依赖使 Continue 返回 `BLOCKED_DEPENDENCIES`，事务不创建空 Round、不取消 Review 可见的 pending task；随后 FINISH failed 写出 PlanFailed 并以 `plan_finished` 取消剩余任务。Root 单测验证只复审一次：可改为 Replan；重复 Continue 或仍被阻断的 Replan 都收敛到 FINISH failed。非法/循环 DAG 仍由 DAG validator 拒绝。 |
| A11 | 已覆盖 | `test_approved_worker_effect_resumes_once_after_full_app_restart` 在同一 PG schema 中先运行 Worker 至审批 interrupt，原子提交批准与 Resume Job 后重建 checkpointer、Worker Job repository、工具执行器和 operation ledger；新 Worker 从 PostgreSQL 领取同 execution/thread 的恢复 Job，从 checkpoint 找回 interrupt，执行工具一次并提交终态。测试核对 Approval、Worker Job、ToolOperation 与 TaskExecution 最终状态。 |
| A12 | 已覆盖 | `test_approved_worker_effect_resumes_once_after_full_app_restart` 注入工具副作用与 ToolOperation 已提交、Worker checkpoint 尚未前进时崩溃，确认 interrupt 仍挂起、Resume Job lease 可接管并从账本重放结果，工具不重发；`test_root_resume_recovers_checkpoint_written_before_run_arm` 验证 Root interrupt 已持久化、Run 行尚未登记时可找回；`test_root_review_reuses_decision_after_commit_before_checkpoint` 验证 Root Review 命令事务已提交但图节点尚未 checkpoint 时重放，决议不重复生成、计划事件只写一次。 |
| A13 | 已覆盖 | `test_resolving_one_worker_approval_only_unblocks_its_execution` 验证 Worker 审批与 Stop 竞争；`test_stop_racing_last_worker_terminal_event_leaves_no_root_resume` 验证终态事件与 Stop 竞争；`test_stop_racing_continue_or_revise_cancels_all_worker_jobs` 分别验证 Continue/Revise 与 Stop。各顺序都以 Run 行事务锁裁决：Stop 获胜时命令被拒绝，Review 获胜时 Stop 随后取消新任务和 Job；不遗留活动审批、可领取 Worker Job 或 Root Resume。 |
| A14 | 已覆盖 | 真实浏览器验收覆盖 Plan 创建前停止与活跃 Worker 停止；`test_run_cancellation_converges_after_expired_worker_lease` 覆盖并行执行及待审批收敛；`test_stop_converges_during_planning_review_and_finalization` 在 PG 验证无 Plan 的规划中停止立即取消、Review 中先进入 CANCELLING 并撤销 Root Resume 后收敛、Plan 已完成但 Run 仍在 Finalize 时立即取消 Run，且没有待恢复 Root Job。 |
| A15 | 已覆盖 | `test_stop_preserves_completed_and_failed_run_without_duplicate_events` 在 PG 对 completed/failed Run 各重复 Stop 两次，确认原状态、取消 epoch 和外部效果状态不变且不产生 Outbox 事件；`test_run_cancellation_converges_after_expired_worker_lease` 另验证 cancelled 终态重复 Stop 不增加取消事件或推进 epoch。 |
| A16 | 已覆盖 | `PostgresGraphThreadLock` 在 Root 与 Worker Graph 调用期间持有按稳定 thread ID 区分的 PostgreSQL session advisory lock；`test_postgres_worker_thread_lock_serializes_separate_app_instances` 验证独立 app 连接不能并发进入同 thread，`test_dispatcher_holds_thread_lock_for_graph_invocation` 验证 Worker 调用受锁保护。`test_expired_worker_lease_is_reclaimed_once_with_new_fence` 与 `test_worker_restart_reclaims_failed_attempt_without_closing_round` 覆盖过期 lease 接管、fence 提升及旧结果拒绝。 |
| A17 | 已覆盖 | `test_external_effect_survives_cancel_as_unknown_and_is_audited` 以不可取消 API stub 模拟远端接收、回执丢失和 Stop；PG 保留 `unknown` 与 executing ToolOperation，且拒绝重发。Run Chain 对 unknown 显示“外部副作用结果未知；停止不能保证撤回已发送的操作”；前端类型检查和生产构建通过。真实第三方 API 联调仍未执行。 |
| A18 | 已覆盖 | 本轮浏览器断线期间后台持续写入 PostgreSQL，重连后恢复完成消息且保留原事件时间；`test_sse_replays_from_last_event_id_and_cleans_up` 注入 replay/实时队列相同序号，验证只发一次且后续事件继续交付。此前 Run Chain 浏览器验收验证审批、Plan Version/DAG、Round/Execution 在刷新后从持久快照恢复。 |
| A19 | 已覆盖 | `test_final_answer_and_memory_job_survive_completion_receipt_loss` 在同一 PostgreSQL Run 完成事务中写入最终回答的两条 Outbox、RunCompleted Outbox、唯一 memory job 和活动命令终态；模拟事务提交后回执丢失，再由新 Outbox dispatcher 投递，并模拟 Memory Worker claim 后崩溃/恢复，最终每类记录仅一份且 Run/命令/记忆 Job 收敛。 |
| A20 | 已覆盖 | 单用户身份隔离按产品决策不适用。`test_cross_run_approval_reference_is_rejected_without_side_effects` 在 PG 拒绝跨 Run 审批和跨 Task 冒用，确认拒绝无决议/Outbox 副作用且正确归属仍能审批；`test_worker_result_reference_from_another_run_is_not_read` 确认其他执行的引用不能通过当前 Worker execution state 读取存储。 |

该盘点表用于安排 P6 后续工作，不把相邻测试或成功的真实 Provider 闭环当作整条矩阵通过。A12/A18 已有对应的 PostgreSQL 故障注入、事件流协议和真实浏览器证据；单用户身份映射仍按产品决策不适用。
