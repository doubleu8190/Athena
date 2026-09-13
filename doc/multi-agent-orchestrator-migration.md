# Multi-Agent 中心编排迁移方案

## 1. 目标与原则

将当前“主 Harness 通过工具自由派生子 Harness”的模型迁移为：

```text
用户 -> Gateway/Command -> Runtime Coordinator
                              |
                +-------------+-------------+
                |                           |
             Planner                     Dispatcher
                |                           |
                +---------> Worker Pool <---+
                                      |
                              Result Collector
                                      |
                                  Synthesizer
                                      |
                                  用户响应
```

主 agent 负责目标理解、任务拆解、依赖和预算、结果校验与最终汇总。子 agent 只执行一个已定义的 `TaskSpec`，只能把 `WorkerResult` 返回给 Coordinator，不能发现、修改或调用其他子任务。

迁移遵循以下约束：

- 子 agent 之间没有可寻址的通信 API；依赖必须返回给主 agent，由主 agent 重新规划。
- 角色和工具权限由 Runtime 强制执行，提示词只用于补充行为指导，不能作为安全边界。
- 所有跨请求状态进入 SQLite/LangGraph checkpoint；进程内对象只保留并发控制和短期缓存。
- 迁移期间允许旧入口适配到新协议，但不提供运行时回滚到旧架构的路径。
- 结果必须可验证、可重试、可观测；单个 worker 失败不应丢失其他 worker 的结果。

## 2. 当前实现评估

当前代码已有可复用基础，但调度边界不清晰：

1. `athena/runtime/graph_runtime.py:313-321` 将两个委派工具注册到全局 `UnifiedToolManager`，因此工具声明没有角色范围。
2. `athena/runtime/sub_agent.py:164-182` 使用与父 agent 相同的 `UnifiedToolManager` 创建 Harness；`allowed_tools=None` 时子 agent 获取全部工具。
3. `athena/core/harness/harness.py:261-262` 将 `None` 定义为允许全部工具，`prompt/sub_agent.md` 的“禁止递归”只是软约束。
4. `athena/runtime/graph_runtime.py:340-405` 直接在工具 handler 内调度并行任务，返回截断后的字符串/JSON，缺少任务 ID、计划 ID、schema 版本和结果校验。
5. 当前事件只有 `subagent.started/completed/failed`（`athena/contracts/events.py:20-24`），没有计划、任务排队、汇总和重试事件。
6. `AgentRunModel` 目前只表达一个运行（`athena/infrastructure/sqlite/models.py:28-57`）；`parent_run_id` 只存在于事件关联，不能恢复一个未完成的子任务队列。

因此第一优先级不是重写 Harness，而是收回委派权限、建立结构化协议和持久化任务账本。

## 3. 目标模块边界

新增模块建议放在 `athena/runtime/orchestration/`：

```text
orchestration/
├── contracts.py       # Pydantic 输入/输出契约
├── planner.py         # 主 agent 计划提示词、计划解析和校验
├── coordinator.py     # 计划生命周期、依赖检查、重试和汇总编排
├── dispatcher.py      # 并发限制、任务领取、取消/暂停传播
├── worker.py          # Worker Harness 适配器
├── synthesizer.py     # 主 agent 汇总请求和结果验证
└── policies.py        # 角色、工具、预算和深度策略
```

现有模块的迁移职责：

| 现有模块 | 迁移后职责 |
| --- | --- |
| `runtime/langgraph_graph.py` | 增加 `planner -> dispatch -> collect -> synthesize` 图分支；保留请求、附件、记忆节点 |
| `runtime/graph_runtime.py` | 只做依赖组装并暴露 `OrchestrationCoordinator`，删除业务型 handler |
| `runtime/sub_agent.py` | 重命名/改造为 `WorkerExecutor`；保留 run 生命周期和 `Harness` 复用，不再暴露 spawn API |
| `core/tools/providers/agents.py` | 过渡期只构造 planner 专用工具；最终改为 `submit_plan` 或移除工具，改用结构化 LLM 输出 |
| `core/tools/manager.py` | 保持通用，不增加 agent 名称分支；接收由 `ToolPolicy` 过滤后的名称 |
| `infrastructure/sqlite/agent_store.py` | 增加 plan/task/result 的仓储接口和幂等领取/租约 |
| `contracts/events.py` | 增加 plan/task/synthesis 事件类型 |

## 4. 核心数据契约

### 4.1 Planner 输入

```python
class PlannerRequest(BaseModel):
    schema_version: int = 1
    plan_id: str
    root_run_id: str
    session_id: str
    goal: str
    context: str
    constraints: list[str] = []
    available_tool_names: list[str] = []
    max_tasks: int = 6
    max_parallelism: int = 4
```

### 4.2 计划和任务

```python
class TaskSpec(BaseModel):
    task_id: str
    plan_id: str
    title: str
    objective: str
    input_context: str = ""
    expected_output: dict[str, Any]  # JSON Schema 子集
    allowed_tools: list[str] = []
    depends_on: list[str] = []
    max_turns: int = 5
    timeout_seconds: int = 120
    retry_limit: int = 1

class ExecutionPlan(BaseModel):
    schema_version: int = 1
    plan_id: str
    root_run_id: str
    goal: str
    tasks: list[TaskSpec]
    aggregation_strategy: Literal["synthesize", "first_success", "all"] = "synthesize"
    max_parallelism: int = 4
```

Planner 输出必须满足：任务数不超过配置；`task_id` 唯一且属于本计划；依赖无环且只能引用本计划任务；第一版只允许无依赖任务并行，后续再启用按 DAG 层级调度；工具必须属于 planner 允许委派的工具集合；预算必须在服务端上限内。解析失败时不得直接执行任务，应回到主 agent 请求修正，超过修正次数则结束为 `plan_invalid`。

### 4.3 Worker 输出

```python
class WorkerResult(BaseModel):
    schema_version: int = 1
    plan_id: str
    task_id: str
    run_id: str
    status: Literal["completed", "failed", "timeout", "cancelled", "invalid_output"]
    output: dict[str, Any] = {}
    raw_text: str = ""
    evidence: list[dict[str, Any]] = []
    uncertainties: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    turn_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
```

Worker 必须通过模型原生 Structured Output 按 `expected_output` 返回结果，同时在系统提示词中重复约束输出格式。Runtime 使用 JSON Schema 子集做二次校验；不支持 Structured Output 的模型不能启动 Multi-Agent 编排。校验失败保存 `invalid_output` 和 `raw_text`，由主 agent 决定是否按任务策略重试，不降级为自由文本成功结果。

### 4.4 汇总输入/输出

`SynthesisRequest` 包含原始目标、计划、按 `task_id` 排序的 `WorkerResult`、失败任务和约束。主 agent 的最终回答仍走现有回答流；内部汇总结果额外保存 `source_task_ids`，以便追踪依据。汇总失败时保留 worker 结果并发出可读的部分完成状态。

## 5. 角色与工具权限

定义不可由模型修改的 `AgentRole`：`planner`、`worker`、`synthesizer`。同一个主运行可以先执行 planner，再执行 synthesizer，但每个 Harness 实例只绑定一个 role。

```python
ROLE_TOOLS = {
    "planner": {"submit_plan"},
    "worker": {"read_local_file", "search_local_file", ...},
    "synthesizer": set(),  # 只读取结果，不执行外部工具
}
```

实施要求：

1. `SubAgentManager.spawn()` 的 `allowed_tools` 默认改为显式空集合或 `WorkerPolicy.default_tools()`，禁止 `None=全部工具`。
2. `spawn_sub_agent`、`spawn_parallel_agents` 只安装到 planner 专用工具视图；worker 的 `get_langchain_tools()` 结果中不得包含这些名称。
3. 在 `ToolContext` 增加 `agent_role`、`plan_id`、`task_id`、`depth`，由 Runtime 注入；工具 handler 校验 role，不接受模型传入的这些字段。
4. `UnifiedToolManager.call_tool()` 增加可选的 policy 校验入口，但不写具体工具名分支；策略由 `ToolPolicy` 注入。
5. `depth` 服务端固定为主运行 `0`、worker `1`，大于 `1` 直接返回 `delegation_forbidden`。这条规则独立于提示词。

## 6. LangGraph 迁移后的流程

现有 `prepare_context -> agent_loop` 改为：

```text
prepare_context
      |
planner_loop (planner tools / structured plan)
      |-- direct_answer --> finalize_response
      |-- execution_plan -> persist_plan -> dispatch_tasks
                                      |
                                  collect_results
                                      |
                              synthesize_loop
                                      |
                                post_process_turn
                                      |
                                finalize_response
```

节点职责：

- `planner_loop`：调用主 agent，允许直接回答或生成 `ExecutionPlan`；不执行 worker。
- `persist_plan`：事务性保存 plan/tasks，并发布 `plan.created`、`task.queued`。
- `dispatch_tasks`：按依赖层级和 `max_parallelism` 领取任务；每个 task 由一个 `WorkerExecutor` 执行。
- `collect_results`：等待所有终态，写入结果，汇总失败/超时/取消；不把 worker 结果直接拼进 LLM 字符串。
- `synthesize_loop`：将结构化结果交给主 agent，主 agent 只负责证据整合和最终表达。

简单请求可由 planner 选择 `direct_answer`，避免所有请求都增加一次 worker 调用。需要用户审批的工具沿用现有 HITL 流程，但审批关联 `task_id` 和 `run_id`，暂停只影响对应 task 及其所属计划。

## 7. 持久化与恢复

在 `athena/infrastructure/sqlite/models.py` 增加三张表，采用 `engine.py` 现有的幂等 `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE` 增量方式：

```text
agent_plans
- plan_id PK, session_id, root_run_id, goal, status
- schema_version, plan_json, aggregation_strategy
- created_at, updated_at, error_json

agent_tasks
- task_id PK, plan_id FK, task_index, status
- depends_on_json, task_json, worker_run_id
- attempt, available_at, claimed_at, started_at, finished_at
- lease_owner, error_json

agent_task_results
- task_id PK, plan_id, worker_run_id
- status, result_json, raw_text, output_hash
- created_at, completed_at
```

需要的索引/约束：`(plan_id, status)`、`(status, available_at)`、`worker_run_id` 唯一、`task_id` 幂等写入。`AgentRunModel` 增加可空的 `role`、`plan_id`、`task_id`、`depth` 字段，旧 run 全部按 `planner` 或 `legacy` 解释。

恢复规则：启动时将过期 `claimed` task 重新排队；若 worker run 已有终态结果则只补写 task 状态，不重复执行；计划恢复从最近 checkpoint 的 `plan_id` 读取数据库任务，不依赖进程内 `_sub_counter`。取消计划时原子标记未完成任务，已运行 worker 通过共享 stop signal 停止。

## 8. 事件契约与客户端兼容

新增 Durable 事件：

```text
plan.created / plan.invalid / plan.completed / plan.failed
task.queued / task.started / task.progress / task.completed / task.failed / task.retrying
synthesis.started / synthesis.completed / synthesis.failed
```

每个事件统一携带 `session_id`、`run_id`、`parent_run_id`、`plan_id`、`task_id`（适用时）、`schema_version` 和幂等 `transition_id`。现有 `subagent.started/completed/failed` 在过渡期作为兼容别名发布，payload 增加 `plan_id/task_id/role`；前端继续按 `run_id` 归组即可，无需一次性改 UI。稳定运行后再删除旧事件别名。

## 9. 分阶段实施

### Phase 0：观测与能力检查

- 启动时校验 Planner、Worker 和 Synthesizer 使用的所有模型均支持 Structured Output。
- 为现有委派事件补充 `role/depth` 日志，统计递归尝试、工具暴露、任务失败和 token/turn 成本。
- 增加策略单元测试，证明 worker 工具列表不含委派工具。

验收：不改变用户行为；可以从日志还原父运行和每个子运行的关系。

### Phase 1：先收回权限

- 引入 `AgentRole`/`ToolPolicy`，worker 默认 deny delegation。
- `SubAgentManager` 改为 `WorkerExecutor` 的兼容外观；显式传入 worker tool allowlist。
- 保留旧两个工具，但只允许 planner role 调用；递归调用返回结构化 `delegation_forbidden`。

验收：递归深度永远不超过 1；旧测试和现有简单委派场景通过。

### Phase 2：结构化计划和任务账本

- 新增 `orchestration/contracts.py`、plan/task/result 表和 `AgentStore` 方法。
- 新增 `submit_plan` 兼容工具；旧工具 handler 将单任务/并行任务转换为 `ExecutionPlan`。
- `Dispatcher` 采用数据库领取 + asyncio semaphore，支持超时、重试和幂等结果。

验收：进程重启后任务可恢复；部分失败、重复投递和取消都有确定终态。

### Phase 3：LangGraph 主流程切换

- 增加 planner、persist、dispatch、collect、synthesizer 节点。
- `orchestrated` 模式下 planner 默认输出结构化计划；主 agent 汇总统一使用 `SynthesisRequest`。
- 对比迁移前基线与新架构的最终答案、延迟、token 和错误率。

验收：直接回答、无依赖并行任务、审批、暂停/恢复、SSE 重连全部通过。

### Phase 4：切换与清理

- 完成验收后直接切换到中心编排流程；失败按新状态机结束，不回退旧架构。
- 旧事件和工具进入弃用期。
- 删除 `spawn_parallel_agents` 自由字符串协议、子 agent 的全工具继承和旧 handler；保留数据读取兼容。

## 10. 测试与验收矩阵

必须新增或改造以下测试：

- 契约：计划 JSON schema、未知字段拒绝、重复 task ID、循环依赖、预算上限。
- 权限：planner 可派发；worker/synthesizer 无委派工具；模型伪造 role/plan/task 字段无效；depth>1 被拒绝。
- 调度：并发上限、依赖层级、超时、单任务重试、部分失败、重复领取和幂等结果。
- 恢复：进程崩溃、过期 lease、已有 worker 终态、LangGraph checkpoint 与 SQLite 不一致。
- 事件：顺序、`session_seq`、`transition_id` 去重、SSE Last-Event-ID 重放。
- 用户流程：直接回答、单 worker、并行 worker、审批工具、暂停/取消、断线重连。

建议的发布指标：计划解析成功率 >= 99%；递归委派率 = 0；任务重复执行率 < 0.1%；worker 结果可追溯率 100%；灰度期间最终答案错误率和 P95 延迟不高于 legacy 基线 10%。

## 11. 发布约束

本次迁移不实现 legacy 回滚。切换后所有新计划只按新状态机完成、失败或取消；数据库迁移仍必须幂等，任务领取和结果写入仍必须支持崩溃恢复与重复投递。

## 12. 推荐的首个实现切片

第一个 PR 只做 Phase 1：`ToolPolicy`、`AgentRole`、worker 委派工具隔离、depth 校验和测试，不改 LangGraph 图。第二个 PR 引入 `TaskSpec/WorkerResult` 与任务表，继续由旧 handler 触发。第三个 PR 才切换 planner/dispatcher/synthesizer 图节点。这样可以先消除架构风险，再逐步改变执行流程，避免一次迁移同时影响 SSE、审批、恢复和前端投影。
