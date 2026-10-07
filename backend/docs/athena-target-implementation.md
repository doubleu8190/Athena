# Athena 目标架构实施文档

## 1. 文档定位

本文是 Athena 目标架构的实施基线，面向最终实现，不描述当前旧代码，也不设计兼容层或迁移路径。

本文只依据两份目标设计：

1. Root Agent + Worker Agent 架构设计。
2. Plan DAG 最终设计。

最终系统必须实现以下链路：

    User
      ↓
    Root Agent
      ├── Direct Answer
      ├── Ordinary Tool Call
      └── Plan DAG
              ↓
          TaskExecutor
              ↓
          Worker Agent
              ↓
          Task Result
              ↓
          DAG Progress
              ↓
          All Tasks Terminal
              ↓
          run.resume
              ↓
          Root Agent Resume
              ↓
          Final Answer

本文规定领域模型、状态、数据库、LangGraph、TaskExecutor、结果传递、事务、审批、取消、重试、事件、API、测试和验收标准。

---

## 2. 最终架构边界

### 2.1 组件职责

| 组件 | 职责 |
| --- | --- |
| Root Agent | 理解用户请求，直接回答，调用普通工具，生成 Plan DAG，最终汇总结果 |
| Worker Agent | 执行一个 Task 的推理、工具调用、审批和结构化输出 |
| Plan | 描述复杂任务的节点和依赖 |
| DAG | 根据边关系确定 Task 执行顺序、Ready 条件和失败传播 |
| Task | 一个具体的工作定义、执行状态和结果载体 |
| TaskExecutor | 计算 Ready Task、启动 Worker、推进 DAG、判断 Plan 完成、创建 run.resume |
| Command Runtime | 获取 Run 锁、执行 Command、启动/恢复 Graph、管理生命周期 |
| ContextAssembler | 为 Root 或 Worker 组装上下文 |
| Capability / Tool | 为 Agent 提供实际外部能力 |
| PostgreSQL | 保存 Run、Plan、Task、Approval、Command、Event 等业务事实 |
| LangGraph Checkpoint | 保存 Root 或 Worker 的 Agent 执行状态 |
| SSE | 展示持久化事件，不参与推理和调度 |

### 2.2 明确不引入的组件

以下概念不属于目标实现：

- Supervisor Agent
- Plan Dispatcher
- Plan Result Synthesizer
- Recovery Agent
- 独立 Workflow Engine
- 独立 Scheduler
- Task Lease
- Task Heartbeat
- Task Attempt History
- 独立 Task Result 表
- 动态 Plan Revision
- SubPlan
- Task Group
- Condition Node
- Loop Node
- Worker 直接恢复 Root

### 2.3 核心不变量

实现必须始终满足：

1. Root Graph 和 Worker Graph 是独立的 LangGraph 流程。
2. Root checkpoint thread 是 run:{run_id}。
3. Worker checkpoint thread 是 task:{task_id}:exec:{execution_generation}。
4. Plan 使用 nodes + edges 表达 DAG。
5. Task 不保存依赖关系。
6. 依赖关系只保存在 agent_plans.plan_json。
7. LLM 只负责生成 Plan，不负责中间 DAG 调度。
8. Root 不参与中间 Task 调度。
9. TaskExecutor 不是长期运行的后台 Scheduler。
10. Worker 只执行自己的 Task，不修改其他 Task、Plan 或 Run。
11. Task 结果先写入 PostgreSQL，再启动依赖它的 Task。
12. 中间 Task 完成不会恢复 Root。
13. 只有全部 Task 进入终态后才创建 run.resume。
14. run.resume 不携带 Task 结果。
15. Root 恢复后必须重新从 PostgreSQL 读取 Plan 和 Task。
16. 审批恢复只恢复指定 Worker。
17. Task 失败不是 Run 失败。
18. 依赖失败会递归取消后继 Task。
19. Task 重试保留 task_id，增加 execution_generation。
20. PostgreSQL 是业务事实来源，事件和进程内通知只能用于唤醒。
21. LangGraph checkpoint 只保存 Agent 执行状态。
22. SSE 只展示持久化事件。
23. Plan 创建后固定，Worker 不能修改 Task 或 Edge。
24. V1 不支持条件 Edge、循环、Task Group、SubPlan 或动态 Plan。

---

## 3. 领域模型

### 3.1 Plan

Plan 定义为：

    Plan = Nodes + Edges

Plan 的持久化结构：

    {
      "version": 1,
      "nodes": [
        {"task_id": "analyze_code"},
        {"task_id": "analyze_db"},
        {"task_id": "synthesize"}
      ],
      "edges": [
        {"from": "analyze_code", "to": "synthesize"},
        {"from": "analyze_db", "to": "synthesize"}
      ]
    }

边的方向固定为：

    from = 前置 Task
    to   = 后继 Task

如果 Task A 依赖 Task B，表示为：

    B → A

Plan JSON 只保存图结构。Task 的详细工作定义和运行结果保存到 agent_tasks。

### 3.2 Task

Task 是 DAG 中的一个 Node，也是 Runtime 的最小执行单元。

工作定义：

    task_id
    plan_id
    run_id
    title
    objective
    input_json
    expected_output_json
    allowed_tools_json
    worker_type

运行字段：

    status
    output_json
    error_json
    execution_generation
    worker_thread_id
    llm_turn_count
    created_at
    updated_at

Task 不包含：

    depends_on
    dependency_json
    parent_task_id
    worker_run_id
    lease_owner
    attempt

### 3.3 Run

Run 表示一次用户请求的完整生命周期，一个 Run 最多创建一个 Plan。

简单请求可以没有 Plan：

    Run
      └── Root Agent

复杂请求为：

    Run
      ├── Root Agent
      └── Plan
            └── Tasks

### 3.4 Worker 执行代次

同一 Task 的每次独立执行都拥有独立执行代次：

    task_id = task_a
    execution_generation = 1
    worker_thread_id = task:task_a:exec:1

重试后：

    task_id = task_a
    execution_generation = 2
    worker_thread_id = task:task_a:exec:2

旧代次的 Worker 结果不能覆盖新代次结果。

---

## 4. 状态模型

### 4.1 Run 状态

    created
    running
    completed
    failed
    cancelled

Run 没有 waiting_approval、root_suspended 或 paused 状态。

Root 等待 Task 时 Run 仍然是 running。等待状态由 Root checkpoint、Task 状态和等待元数据共同表达。

### 4.2 Plan 状态

    created
    running
    completed
    failed
    cancelled

Plan 为 completed 只表示所有 Task 都进入终态，不表示所有 Task 都成功。

### 4.3 Task 状态

    pending
    running
    waiting_approval
    done
    failed
    timed_out
    cancelled

终态：

    done
    failed
    timed_out
    cancelled

允许的主要转换：

    pending → running
    pending → cancelled
    running → waiting_approval
    running → done
    running → failed
    running → timed_out
    running → cancelled
    waiting_approval → running
    waiting_approval → cancelled

终态不能再次转换。Retry 是一个受控业务操作，会增加执行代次并把 Task 重新置为 pending。

### 4.4 Command 状态

    queued
    running
    completed
    failed
    cancelled

Command 状态只描述 Command 自己是否完成，不代表 Run、Plan 或 Task 的状态。

---

## 5. Plan DAG 领域模块

建议新增：

    athena/planning/models.py
    athena/planning/validator.py
    athena/planning/dag.py
    athena/planning/__init__.py

### 5.1 数据模型

建议模型：

    class PlanNodeRef(BaseModel):
        task_id: str

    class PlanEdge(BaseModel):
        from_task_id: str
        to_task_id: str

    class PlanDocument(BaseModel):
        version: Literal[1] = 1
        nodes: list[PlanNodeRef]
        edges: list[PlanEdge]

Task 定义单独建模：

    class TaskDefinition(BaseModel):
        task_id: str
        title: str
        objective: str
        input: dict[str, Any]
        expected_output: dict[str, Any]
        allowed_tools: list[str]
        worker_type: str = "general"

Plan 图结构与 Task 工作定义必须分离。

### 5.2 Plan 验证

PlanValidator 在任何数据库写入前运行，检查：

1. Plan 版本是 1。
2. Node 数量大于零。
3. Task ID 唯一。
4. Task Definition 与 Node 集合完全一致。
5. Edge 起点存在。
6. Edge 终点存在。
7. 不允许自依赖。
8. 不允许重复 Edge。
9. 不允许形成环。
10. max_parallelism 合法。
11. Worker 类型已经注册。
12. 工具属于合法工具集合。
13. Task 标题、目标和输出约束满足格式限制。

环检测必须是确定性算法，推荐 Kahn 拓扑排序：

1. 计算每个节点入度。
2. 将入度为零的节点加入队列。
3. 删除节点并降低后继入度。
4. 如果处理数量小于节点总数，则存在环。

验证失败时：

    不创建 Plan
    不创建 Task
    不启动 Worker
    将结构化验证错误返回 Root Agent

Root 可以在预算内修正并重新提交。超过 Root 重试预算后，Run 进入 failed。

### 5.3 运行时索引

存储仍使用 nodes + edges，运行时转换为：

    dependencies: dict[str, set[str]]
    children: dict[str, set[str]]

例：

    A → C
    B → C
    C → D

转换为：

    dependencies = {
        "A": set(),
        "B": set(),
        "C": {"A", "B"},
        "D": {"C"},
    }

    children = {
        "A": {"C"},
        "B": {"C"},
        "C": {"D"},
        "D": set(),
    }

### 5.4 Ready Task

Ready 条件：

    task.status == "pending"
    and all(predecessor.status == "done"
            for predecessor in dependencies_of(task.task_id))

没有 predecessor 的 Task 是 root-level Task，可以立即运行。

只要 predecessor 中存在以下状态之一：

    failed
    timed_out
    cancelled

当前 Task 就不能运行，必须标记为：

    cancelled
    error.reason = "dependency_failed"

### 5.5 失败传播

失败传播必须递归进行：

    A failed
      ↓
    B cancelled: dependency_failed
      ↓
    C cancelled: dependency_failed

多个 predecessor 中任一个失败，后继就必须取消。

取消错误结构：

    {
      "reason": "dependency_failed",
      "dependency_task_ids": ["task_a"],
      "message": "task cancelled because a dependency failed"
    }

---

## 6. PostgreSQL 数据模型

数据库直接按目标模型重建，不设计兼容字段。

### 6.1 agent_runs

字段：

    run_id
    session_id
    user_id
    status

    request_json
    response_json
    error_json

    root_thread_id
    root_llm_turn_count
    worker_llm_turn_count
    total_worker_llm_turn_count
    tool_call_count

    root_wait_generation
    root_wait_checkpoint_id
    root_wait_armed_at

    created_at
    updated_at

固定关系：

    root_thread_id = run:{run_id}

活动 Run 唯一约束：

    CREATE UNIQUE INDEX one_active_run_per_session
    ON agent_runs(session_id)
    WHERE status IN ('created', 'running');

取消使用 CAS：

    UPDATE agent_runs
    SET status = 'cancelled', updated_at = now()
    WHERE run_id = :run_id
      AND status IN ('created', 'running');

### 6.2 agent_plans

字段：

    plan_id
    run_id
    session_id
    status
    user_goal
    max_parallelism
    plan_json
    created_at
    updated_at

一个 Run 最多一个 Plan：

    CREATE UNIQUE INDEX one_plan_per_run
    ON agent_plans(run_id);

### 6.3 agent_tasks

字段：

    task_id
    plan_id
    run_id

    title
    objective
    input_json
    expected_output_json
    allowed_tools_json
    worker_type

    status
    output_json
    error_json

    execution_generation
    worker_thread_id
    llm_turn_count

    created_at
    updated_at

索引：

    CREATE INDEX idx_agent_tasks_plan_status
    ON agent_tasks(plan_id, status);

    CREATE INDEX idx_agent_tasks_run_status
    ON agent_tasks(run_id, status);

不创建 agent_task_results。

### 6.4 agent_commands

字段：

    command_id
    session_id
    run_id
    command_type
    idempotency_key
    payload_json
    status
    result_json
    error_json
    queued_at
    started_at
    completed_at

唯一约束：

    CREATE UNIQUE INDEX uq_agent_commands_idempotency
    ON agent_commands(command_type, idempotency_key);

### 6.5 agent_events

字段：

    session_id
    session_seq
    run_id
    event_type
    durability
    payload_json
    occurred_at

SSE 重放按：

    session_id + session_seq > last_seen_sequence

### 6.6 approvals

字段：

    approval_id
    session_id
    run_id
    plan_id
    task_id
    worker_thread_id
    tool_call_id
    tool_name
    arguments_json
    risk_level
    status
    decision
    created_at
    decided_at

审批必须绑定具体 Worker Task，不能只绑定 Run。

---

## 7. Repository 和事务接口

Repository 必须以完整业务事务为边界，Runtime 不应通过多个独立字段更新拼接状态转换。

### 7.1 RunRepository

    acquire_run_lock(run_id) -> bool
    release_run_lock(run_id) -> None
    get_run(run_id) -> RunRecord | None
    mark_running(run_id) -> None
    mark_root_wait_armed(run_id, checkpoint_id) -> None
    complete_run(run_id, response) -> None
    fail_run(run_id, error) -> None
    cancel_run(run_id) -> bool

Run advisory lock 防止两个 Root Command 同时恢复同一个 Root Graph。

Worker 执行期间不持有 Run lock。

### 7.2 PlanRepository

    create_plan_with_tasks(plan, tasks) -> None
    get_plan(plan_id) -> PlanRecord | None
    get_plan_with_tasks(plan_id) -> tuple[PlanRecord, list[TaskRecord]]
    mark_running(plan_id) -> None
    mark_completed(plan_id) -> None
    mark_failed(plan_id, error) -> None
    mark_cancelled(plan_id) -> None

Plan 和 Task 创建必须在同一个事务内完成。

### 7.3 TaskRepository

    claim_ready_tasks(plan_id, max_parallelism) -> list[TaskRecord]
    complete_task(task_id, execution_generation, result) -> TaskTerminalOutcome
    fail_task(task_id, execution_generation, error) -> TaskTerminalOutcome
    retry_task(task_id) -> RetryOutcome
    cancel_plan_tasks(plan_id) -> None

complete_task() 和 fail_task() 的事务步骤：

1. 锁定 Task。
2. 校验 execution_generation。
3. 校验当前状态为 running。
4. 写入输出或错误。
5. 写入 Task 终态。
6. 锁定同一 Plan 的 Task 行。
7. 传播依赖失败。
8. 计算新的 Ready Task。
9. 判断是否所有 Task 已终态。
10. 如果全部终态，更新 Plan 并创建幂等 run.resume。
11. 提交事务。

只有事务提交成功后，Runtime 才能启动新的 Worker。

---

## 8. Root Agent Graph

### 8.1 Root 身份

    thread_id = run:{run_id}

Root Graph 保存：

- 用户请求
- Root 对话消息
- Root 工具调用和工具结果
- Plan 提交状态
- 等待 Task 的 interrupt
- 最终结果生成状态

Root Graph 不保存其他 Worker 的完整执行状态。

### 8.2 Root 节点

建议节点：

    prepare_request
    assemble_root_context
    root_agent_loop
    execute_root_tool
    validate_plan_submission
    persist_plan
    arm_root_wait
    load_plan_results
    assemble_result_context
    finalize_root_response

逻辑：

    START
      ↓
    prepare_request
      ↓
    assemble_root_context
      ↓
    root_agent_loop
      ├── direct_answer → finalize_root_response → END
      ├── root_tool_call → execute_root_tool → root_agent_loop
      └── submit_plan
              ↓
          validate_plan_submission
              ↓
          persist_plan
              ↓
          arm_root_wait
              ↓
          interrupt

### 8.3 普通工具调用

普通工具调用只属于 Root Graph：

    Root LLM
      ↓
    Root Tool
      ↓
    Tool Result
      ↓
    Root LLM

普通工具调用不经过 Plan、TaskExecutor 或 Worker。

### 8.4 Plan 提交

提交步骤：

1. Root LLM 产生 submit_plan。
2. 解析 PlanDocument 和 TaskDefinition。
3. 执行 PlanValidator。
4. 检查工具和 Worker 类型。
5. 在一个事务中创建 Plan 和 Tasks。
6. Plan 置为 running。
7. 保存 Root 等待 checkpoint。
8. 返回 root_suspended。

验证失败时，不创建任何持久化 Plan/Task，结构化错误返回 Root Agent。

### 8.5 Root 等待 checkpoint

Root 不能在进程内等待 Task，也不能长期持有数据库连接或 Run lock。

使用 LangGraph interrupt 保存等待状态：

    interrupt({
        "reason": "waiting_for_tasks",
        "plan_id": plan_id,
    })

Root Graph 调用返回后：

1. 读取 Root checkpoint。
2. 确认等待 interrupt 已持久化。
3. 写入 root_wait_checkpoint_id。
4. 增加 root_wait_generation。
5. 写入 root_wait_armed_at。
6. Command 以 root_suspended 完成。
7. 释放 Run advisory lock。
8. TaskExecutor 启动 Ready Task。

如果 Worker 早于 root_wait_armed_at 写入完成，Worker 完成逻辑不能直接创建 run.resume。Root 退出前必须补偿检查，确认等待状态已经 armed 且所有 Task 已终态后，再创建恢复命令。

### 8.6 Root 恢复

run.resume 恢复：

    恢复 run:{run_id}
      ↓
    从 PostgreSQL 读取 Plan
      ↓
    从 PostgreSQL 读取全部 Task
      ↓
    组装结果上下文
      ↓
    Root Agent 生成最终回答
      ↓
    写入 response_json
      ↓
    Run = completed

run.resume payload 不能携带 Task 结果。

---

## 9. Worker Agent Graph

### 9.1 Worker 身份

每个 Task 每个执行代次拥有独立 thread：

    task:{task_id}:exec:{execution_generation}

### 9.2 Worker State

Worker State 至少包含：

    run_id
    plan_id
    task_id
    execution_generation

    task
    upstream_results

    messages
    pending_tool_calls
    tool_results

    approval_batch
    approval_decisions

    turn_count
    final_result
    error

其中：

- task 是 Task 工作定义。
- upstream_results 是直接 predecessor 的结果。
- messages、工具调用和审批状态属于 Worker checkpoint。
- final_result 只代表 Worker 的最终输出，业务状态由 TaskExecutor 提交。

### 9.3 Worker 节点

    load_task_context
      ↓
    worker_llm
      ├── final_result → validate_result → END
      ├── tool_call → approval_gate
      │                    ├── approval_required → interrupt
      │                    └── approved → execute_tool → worker_llm
      └── retryable_error → worker_llm

Worker 可以调用工具、执行多轮 LLM、等待审批和恢复 checkpoint，但不能：

- 创建 Plan
- 创建 Task
- 修改其他 Task
- 修改 Run
- 恢复 Root
- 决定 Plan 是否完成
- 生成最终用户回答

---

## 10. Task 之间的信息传递

### 10.1 Task 不等于 LangGraph Node

禁止把整个 Plan 动态构造成一个 LangGraph：

    Root Graph
      ├── task_a_node
      ├── task_b_node
      └── task_c_node

正确结构：

    Plan DAG
      ├── Task A → Worker Graph A
      ├── Task B → Worker Graph B
      └── Task C → Worker Graph C

Plan DAG 的拓扑由 TaskExecutor 管理，单个 Worker 的内部步骤由 Worker Graph 管理。

### 10.2 结果持久化

假设：

    B → A

Worker B 返回结构化结果：

    {
      "task_id": "task_b",
      "status": "done",
      "output": {
        "findings": [
          {
            "category": "slow_query",
            "file": "UserRepository.java",
            "line": 42,
            "reason": "missing index"
          }
        ]
      },
      "evidence": [],
      "uncertainties": []
    }

TaskExecutor 在事务中写入：

    agent_tasks.output_json = result.output
    agent_tasks.status = done

提交成功后，数据库结果才成为下游输入来源。

### 10.3 构造下游输入

A Ready 后：

1. 根据 PlanDAG.dependencies_of("task_a") 找到 B。
2. 查询 B 的 output_json。
3. 按稳定的 task_id 顺序组装 upstream_results。
4. 将结果写入 Worker A 初始状态。
5. 启动 task:task_a:exec:1。

Worker A 输入：

    {
      "task": {
        "task_id": "task_a",
        "objective": "根据数据库分析结果归纳性能问题"
      },
      "upstream_results": {
        "task_b": {
          "status": "done",
          "output": {
            "findings": [
              {
                "category": "slow_query",
                "file": "UserRepository.java",
                "line": 42,
                "reason": "missing index"
              }
            ]
          }
        }
      }
    }

链路：

    Worker B
      ↓
    TaskExecutor
      ↓
    PostgreSQL agent_tasks.output_json
      ↓
    TaskExecutor 读取 B
      ↓
    构造 Worker A TaskContext
      ↓
    Worker A 初始 checkpoint
      ↓
    Worker A 推理

Worker 之间不能直接调用，也不能通过进程内全局变量传递结果。

### 10.4 直接前置节点

默认只向 Task 传递直接 predecessor 的结果。

    A → B → C

C 默认接收 B 的结果。如果 C 必须直接使用 A 的结果，Plan 必须显式声明：

    A → C
    B → C

这样每个结果来源都是可审计、可重建的。

### 10.5 多个前置节点

    B → A
    C → A

A 接收：

    {
      "upstream_results": {
        "task_b": {"status": "done", "output": {}},
        "task_c": {"status": "done", "output": {}}
      }
    }

组装顺序必须稳定，不能依赖 Set 遍历顺序。

### 10.6 大结果

小结果直接保存在 output_json。

大结果使用 artifact 引用：

    {
      "summary": "...",
      "artifacts": [
        {
          "artifact_id": "artifact-123",
          "kind": "file",
          "location": "..."
        }
      ]
    }

下游 Task 接收摘要和引用，通过允许的 Capability/Tool 读取详细内容，避免把大对象重复写入多个 checkpoint。

---

## 11. TaskExecutor

### 11.1 定位

TaskExecutor 是 Runtime Service，不是 Agent、Workflow Engine 或长期 Scheduler。

它只在两个时机工作：

1. Root 提交 Plan 后，启动初始 Ready Task。
2. Worker 进入终态后，推进 DAG 并启动新的 Ready Task。

### 11.2 核心接口

    start_ready_tasks(plan_id) -> TaskExecutionOutcome
    on_task_terminal(task_id, execution_generation, result) -> None
    check_plan_completion(plan_id) -> None

### 11.3 启动 Ready Task

    start_ready_tasks(plan_id)
      ↓
    读取 Plan 和 Task
      ↓
    数据库事务内锁定 Plan
      ↓
    计算当前 running 数量
      ↓
    计算 Ready Task
      ↓
    限制 max_parallelism
      ↓
    pending → running CAS
      ↓
    写入 execution_generation 和 worker_thread_id
      ↓
    提交事务
      ↓
    启动 Worker Graph

并发调用时，只有成功完成 CAS 的调用可以启动该 Task。

### 11.4 处理 Worker 终态

    Worker 完成
      ↓
    TaskExecutor.on_task_terminal
      ↓
    校验 task_id + execution_generation
      ↓
    事务写入 Task 终态和结果
      ↓
    失败传播
      ↓
    重新计算 Ready Task
      ↓
    判断是否全部终态
      ↓
    提交事务

事务提交后：

- DAG 未完成：启动新的 Ready Worker。
- DAG 已完成：Plan 置为 completed，创建幂等 run.resume。

TaskExecutor 不恢复 Root Graph。

### 11.5 最后一个 Task

最后一个 Task 的判断必须在事务内锁定同一 Plan 的 Task 行后重新查询，不能使用调用方内存中的旧列表。

事务内：

1. 锁定 Plan。
2. 锁定 Plan 下全部 Task。
3. 写入当前 Task 终态。
4. 传播依赖失败。
5. 重新查询全部 Task。
6. 如果全部为终态，Plan 置为 completed。
7. 插入幂等 run.resume。
8. 否则计算 Ready Task。
9. 提交事务。

恢复命令的 idempotency key 必须稳定，例如：

    run:{run_id}:plan:{plan_id}:wait:{root_wait_generation}

重复插入唯一约束冲突应视为幂等成功。

### 11.6 Root 等待竞态

Task 可能在 Root 退出前完成。解决顺序：

1. Root 保存 checkpoint。
2. Root 写入等待元数据。
3. Root 释放 Run lock。
4. TaskExecutor 只在等待元数据存在时创建 run.resume。

如果 Worker 已经完成但等待元数据尚未存在：

- 保留 Task 终态。
- 不创建有效 run.resume。
- Root 退出前执行补偿检查。
- 补偿检查发现全部 Task 终态且等待已 armed 后，创建恢复命令。

---

## 12. Command Runtime

### 12.1 Command 类型

至少支持：

    run.start
    run.resume
    run.cancel
    approval.resolve

Task Retry 可以先更新 Task，再创建 run.resume，不需要独立调度命令。

### 12.2 run.start

    获取 Run advisory lock
      ↓
    Run = running
      ↓
    执行 Root Graph

结果：

- 直接回答：Run completed。
- 提交 Plan：Root suspended，Run 仍 running。
- Worker 审批等待：Task waiting_approval，Run 仍 running。
- 最终回答：Run completed。
- 无法继续：Run failed。

### 12.3 run.resume

处理顺序：

1. 获取 Run advisory lock。
2. 重新读取 Run、Plan 和全部 Task。
3. Run 已取消或完成时安全跳过。
4. 存在可运行 Pending Task 时，调用 start_ready_tasks。
5. 如果启动了 Task，Command 以 tasks_started 完成，不恢复 Root。
6. 全部 Task 已终态时，Plan 置为 completed。
7. 恢复 Root Graph run:{run_id}。
8. Root 从 PostgreSQL 读取结果。
9. Root 生成最终回答。
10. 写入 response_json。
11. Run 置为 completed。
12. 释放 Run lock。

### 12.4 幂等

所有自动生成的 run.resume 通过 idempotency_key 插入。

重复插入产生唯一约束冲突时视为幂等成功，不能导致 Run 失败。

### 12.5 不自动接管

V1 不使用：

- Task lease
- Task heartbeat
- last_seen_at
- takeover_count
- 自动扫描并接管 Worker
- 进程重启后无条件恢复运行中 Task

恢复必须由持久化 Command、明确的 checkpoint 恢复或用户操作触发。

---

## 13. 审批

审批属于 Worker 执行过程：

    Worker LLM 请求高风险工具
      ↓
    approval_gate
      ↓
    写入 approvals
      ↓
    Task = waiting_approval
      ↓
    Worker interrupt checkpoint

审批期间：

    Run = running
    Plan = running
    Task = waiting_approval

用户审批后：

1. 解析 Approval。
2. 校验 Run、Plan、Task 和 Worker thread 归属。
3. 使用 Worker thread 恢复 Worker Graph。
4. 将审批决定传入 Worker。
5. Worker 继续执行。
6. Worker 完成后调用 on_task_terminal。

审批恢复不能：

- 恢复 Root
- 获取 Run lock
- 修改其他 Task
- 修改 Plan 状态

只有整个 Plan 终态后才创建 run.resume。

---

## 14. 取消

Run 取消使用 CAS：

    UPDATE agent_runs
    SET status = 'cancelled', updated_at = now()
    WHERE run_id = :run_id
      AND status IN ('created', 'running');

同一事务中：

    Plan → cancelled
    pending Task → cancelled
    waiting_approval Task → cancelled

正在运行的 Worker 使用 stop signal 停止。

Worker 完成与取消竞争时，只有满足以下条件的写入有效：

- task_id 相同
- execution_generation 相同
- Task 当前仍为 running
- Run 当前没有取消

取消成功后不能创建有效 run.resume。已完成 Task 的结果保留。

---

## 15. Task Retry

只允许重试：

    failed
    timed_out
    cancelled

重试事务：

1. 锁定 Task。
2. 检查当前状态是可重试终态。
3. execution_generation += 1。
4. 清空 output_json 和 error_json。
5. 设置 status = pending。
6. 生成新的 Worker thread。
7. 创建幂等 run.resume。

重试后：

    task_id 保持不变
    execution_generation 改变
    worker_thread_id 改变

run.resume 发现 Pending Task 后只启动 Worker，不恢复 Root。新的 Worker 完成且 Plan 全部终态后，才恢复 Root。

---

## 16. ContextAssembler

Root 和 Worker 不应自行拼接复杂上下文。建议新增：

    athena/context/assembler.py

### 16.1 RootContext

    system_context
    user_message
    conversation
    memory
    knowledge
    available_capabilities

### 16.2 WorkerContext

    task_definition
    static_input
    direct_predecessor_results
    expected_output
    allowed_tools
    relevant_files
    relevant_knowledge

Worker 默认不接收完整 Conversation、Memory 或 Knowledge Base。是否注入由 Task 定义和 ContextAssembler 决定。

### 16.3 Task 输入分类

Task 输入必须区分：

1. Root 创建时确定的静态输入。
2. DAG predecessor 产生的动态输入。
3. Worker Graph 自己的执行状态。

推荐形式：

    TaskContext(
        task=task_definition,
        static_input=task.input_json,
        upstream_results=predecessor_outputs,
    )

---

## 17. Capability 和 Tool

Agent 访问外部能力的链路：

    Agent
      ↓
    Capability
      ↓
    Tool
      ↓
    External System

Capability 示例：

    File
    Memory
    Knowledge Base
    Browser
    Code Analysis
    Database

Tool 示例：

    file_search
    file_read
    memory_search
    kb_search
    browser_open

Task 的 allowed_tools_json 是 Worker 的工具边界。Worker 不能调用未授权工具。

Root 普通工具和 Worker 工具可以共享底层 Tool 基础设施，但使用不同的上下文、授权范围和审批策略。

---

## 18. 事件和 SSE

事件类型至少包括：

    run.started
    run.root_suspended
    run.resumed
    run.completed
    run.failed
    run.cancelled

    plan.created
    plan.completed
    plan.failed
    plan.cancelled

    task.started
    task.waiting_approval
    task.completed
    task.failed
    task.cancelled

    approval.required
    approval.resolved

推荐在同一事务中完成：

    Task 状态更新
    Task 结果写入
    Task 事件写入
    最后一个 Task 的 run.resume 插入

事件只用于：

- SSE 展示
- 进程内唤醒
- 审计

事件不能作为：

- Task 状态事实
- Task 结果事实
- 恢复依据
- 调度依据

SSE 断线后从 agent_events 按 session_seq 重放。

Task 事件 payload 至少包含：

    run_id
    plan_id
    task_id
    status
    worker_type
    execution_generation
    error

---

## 19. API 边界

建议 API：

    POST /v1/runs
    GET  /v1/runs/{run_id}
    GET  /v1/runs/{run_id}/events

    POST /v1/runs/{run_id}/resume
    POST /v1/runs/{run_id}/cancel

    POST /v1/runs/{run_id}/tasks/{task_id}/approval
    POST /v1/runs/{run_id}/tasks/{task_id}/retry

API 层只做：

- 参数校验
- 身份和权限检查
- 创建持久化 Command
- 返回 202

API 不直接：

- 调用 Root Agent
- 启动 Worker
- 更新 Task 结果
- 恢复 Root
- 判断 DAG 是否完成

---

## 20. 目标代码结构

建议最终收敛为：

    athena/
    ├── api/
    │   ├── runs.py
    │   ├── approvals.py
    │   └── events.py
    ├── agents/
    │   ├── root/
    │   │   ├── graph.py
    │   │   ├── state.py
    │   │   └── nodes.py
    │   └── worker/
    │       ├── agent.py
    │       ├── state.py
    │       ├── graph.py
    │       ├── general.py
    │       └── registry.py
    ├── planning/
    │   ├── models.py
    │   ├── validator.py
    │   └── dag.py
    ├── runtime/
    │   ├── command_consumer.py
    │   ├── run_manager.py
    │   ├── run_lock.py
    │   ├── task_executor.py
    │   ├── approval_manager.py
    │   └── event_manager.py
    ├── context/
    │   └── assembler.py
    ├── persistence/
    │   ├── models/
    │   └── repositories/
    └── infrastructure/
        ├── llm/
        ├── tools/
        ├── checkpoint/
        └── events/

现有 Memory、Knowledge、File、Retrieval、Embedding、LLM Provider 和 Tool 实现可以作为 Capability/Infrastructure 保留，但必须通过 Root/Worker 新边界接入。

---

## 21. 实施顺序

### 阶段一：固定 Contracts

完成：

- PlanDocument
- PlanEdge
- TaskDefinition
- TaskResult
- WorkerContext
- Run/Plan/Task/Command 状态
- Command payload
- Event payload

完成标准：Runtime 和 Graph 不再依赖旧 ExecutionPlan、旧 depends_on 字段或旧 Worker Run 模型。

### 阶段二：实现 DAG 纯逻辑

完成：

- PlanValidator
- PlanDAG
- 环检测
- Ready 计算
- 失败传播
- 终态判断

完成标准：不需要数据库和 LLM 就能测试所有 DAG 行为。

### 阶段三：重建数据库和 Repository

完成：

- agent_runs
- agent_plans
- agent_tasks
- agent_commands
- agent_events
- approvals
- advisory lock
- CAS 状态转换
- run.resume 幂等约束

完成标准：所有状态转换、Task 结果和恢复命令都有明确事务边界。

### 阶段四：实现 Worker Registry 和 Worker Graph

完成：

- WorkerAgent Protocol
- GeneralWorkerAgent
- WorkerAgentRegistry
- WorkerState
- Worker Graph
- Worker checkpoint
- 审批 interrupt/resume

完成标准：单个 Worker 可独立完成 Task、调用工具、等待审批、恢复 checkpoint 并返回结构化结果。

### 阶段五：实现 Root Graph

完成：

- 直接回答
- Root 普通工具调用
- submit_plan
- Plan 校验和持久化
- Root waiting interrupt
- Root 结果收集
- 最终回答

完成标准：Root 提交 Plan 后保存 run:{run_id} checkpoint 并结束当前执行，不在进程内等待 Worker。

### 阶段六：实现 TaskExecutor

完成：

- claim Ready Task
- 启动 Worker
- 处理 Worker 终态
- 结果写入
- 失败传播
- 启动后继 Task
- Plan 完成判断
- run.resume 幂等创建

完成标准：串行、并行、汇聚、分叉、多级 DAG 正确执行。

### 阶段七：重写 Command Runtime

完成：

- Run advisory lock
- run.start
- run.resume
- run.cancel
- approval.resolve
- Retry + run.resume

完成标准：run.resume 遇到 Pending Task 时只启动 Worker，全部 Task 终态时才恢复 Root。

### 阶段八：接入 API 和 SSE

完成：

- Run API
- Approval API
- Retry API
- Cancel API
- Run SSE
- Task/Plan events
- 断线重放

完成标准：客户端可以观察完整 Run/Plan/Task 生命周期。

### 阶段九：删除旧实现

删除：

- PlanDispatcher
- PlanResultSynthesizer
- WorkerExecutor
- RecoveryReconciler
- agent_task_results
- Task lease 字段
- 旧同步计划执行节点
- 旧 Root/Worker 混合 checkpoint 语义

完成标准：旧调度路径不再有可调用入口，数据库也不存在旧字段和旧表。

---

## 22. 测试计划

### 22.1 DAG 单元测试

覆盖：

- Task ID 重复
- 未知 Edge 节点
- 自依赖
- 重复 Edge
- 环检测
- 单节点 Plan
- 串行 DAG
- 并行 DAG
- fan-out
- fan-in
- 多级 DAG
- Ready Task
- 依赖失败传播
- 多个 predecessor 中一个失败
- 全部 Task 终态
- max_parallelism

### 22.2 Repository 集成测试

覆盖：

- Plan 和 Task 原子创建
- pending → running CAS
- running → done CAS
- running → failed CAS
- 旧 execution_generation 被拒绝
- 重复 Task 完成幂等
- 两个 Worker 同时完成最后 Task
- run.resume 唯一约束
- Root 未 armed 时不创建 resume
- Run cancel 与 Worker finish 竞争
- Task retry 增加 generation

### 22.3 Root Graph 测试

覆盖：

- 直接回答
- 普通工具调用循环
- submit_plan
- 非法 DAG 被拒绝
- Plan 持久化
- Root waiting interrupt
- Root checkpoint thread_id
- run.resume 恢复 Root
- Root 从 PostgreSQL 读取结果
- 最终回答

### 22.4 Worker Graph 测试

覆盖：

- 普通 Task 完成
- 多轮 LLM
- 工具调用
- 审批 interrupt
- 审批恢复
- 工具失败
- Worker 超时
- Worker 取消
- 不同 execution_generation 使用不同 thread_id

### 22.5 TaskExecutor 集成测试

至少覆盖：

    A → B
    A → B + C
    A + B → C
    A → B → C
    A → B/C → D
    A → B/C → D → E/F → G

每个场景验证：

- 启动顺序
- 并行度
- 结果传递
- 失败传播
- 终态判断
- run.resume 时机
- Root 恢复时机

### 22.6 API/SSE 测试

覆盖：

- 创建 Run
- 查询 Run
- 查询 Plan/Task
- SSE 初始事件
- SSE 断线重放
- 审批
- 取消
- 重试
- 重复 Command

---

## 23. 端到端验收场景

### 场景一：简单回答

    用户消息
      ↓
    Root Agent
      ↓
    Final Answer
      ↓
    Run completed

不得创建 Plan 或 Task。

### 场景二：普通工具调用

    Root Agent
      ↓
    普通 Tool
      ↓
    Root Agent
      ↓
    Final Answer

不得进入 TaskExecutor。

### 场景三：串行 DAG

    A → B → C

必须满足：

- A 完成后才启动 B。
- B 的 output_json 作为下游输入。
- B 完成后才启动 C。
- C 完成后才创建 run.resume。

### 场景四：并行 DAG

    A → C
    B → C

必须满足：

- A 和 B 并行。
- C 等待 A、B 都 done。
- C 收到 A、B 两份结果。

### 场景五：失败传播

    A → B → C

A 失败后：

    A = failed
    B = cancelled, reason = dependency_failed
    C = cancelled, reason = dependency_failed
    Plan = completed
    run.resume 被创建
    Root 读取失败结果并生成部分失败说明

### 场景六：Worker 审批

    Worker
      → waiting_approval
      → 用户审批
      → Worker resume
      → Task done

审批过程中：

    Run = running
    Plan = running
    Task = waiting_approval

不能恢复 Root。

### 场景七：Task 重试

    Task exec:1 failed
      ↓
    retry
      ↓
    Task exec:2 pending
      ↓
    run.resume
      ↓
    Worker exec:2

必须保留相同 task_id，并使用不同 checkpoint。

### 场景八：取消竞争

用户取消和 Worker 同时完成时，只有一个状态转换可以成功。取消成功后不能产生新的 run.resume。

---

## 24. 最终验收清单

### 架构

- [ ] 只有 Root Agent 和 Worker Agent 两类 Agent。
- [ ] 不存在 Workflow Engine。
- [ ] 不存在独立 Scheduler。
- [ ] 不存在 Synthesizer Agent 或 Recovery Agent。
- [ ] Root 和 Worker 使用独立 checkpoint。

### Plan DAG

- [ ] Plan 使用 nodes + edges。
- [ ] Task 不保存依赖字段。
- [ ] Plan 创建前完成 DAG 校验。
- [ ] 环、未知节点和自依赖被拒绝。
- [ ] Ready Task 由 Runtime 确定性计算。
- [ ] 依赖失败递归传播。
- [ ] Plan 创建后不可动态修改。

### Root

- [ ] Root 能直接回答。
- [ ] Root 能调用普通工具。
- [ ] Root 能提交 Plan。
- [ ] Root 提交 Plan 后保存等待 checkpoint。
- [ ] Root 不等待 Worker。
- [ ] Root 恢复后从数据库读取 Task 结果。

### Worker

- [ ] 每个 Task 有独立 Worker checkpoint。
- [ ] Worker 能执行多轮 LLM 和工具调用。
- [ ] Worker 能等待和恢复审批。
- [ ] Worker 不能修改其他业务对象。
- [ ] Worker 结果由 Runtime 持久化。

### Runtime

- [ ] TaskExecutor 能启动 Ready Task。
- [ ] TaskExecutor 能推进 DAG。
- [ ] TaskExecutor 能限制并行度。
- [ ] TaskExecutor 能处理失败传播。
- [ ] TaskExecutor 只在全部 Task 终态后创建 run.resume。
- [ ] run.resume 具有幂等性。
- [ ] Run lock 防止 Root 并发恢复。

### 数据和恢复

- [ ] PostgreSQL 是业务事实来源。
- [ ] Checkpoint 只保存 Agent 执行状态。
- [ ] Task 结果直接保存在 agent_tasks。
- [ ] 没有 Task lease、heartbeat 或 attempt 历史。
- [ ] Task Retry 使用 execution_generation。
- [ ] 取消和 Worker 完成使用 CAS。

### API 和事件

- [ ] Run API 不直接调用 Agent。
- [ ] Approval API 只恢复 Worker。
- [ ] Retry API 不直接恢复 Root。
- [ ] SSE 支持持久化事件重放。
- [ ] 事件不是调度事实来源。

---

## 25. 一句话定义

Athena 的最终实现必须满足：

> Root Agent 负责理解和规划，Plan DAG 负责描述任务关系，TaskExecutor 负责确定性推进 DAG，Worker Agent 负责执行单个 Task，Task 结果通过 PostgreSQL 在 DAG 节点之间传递，Root 和 Worker 使用独立 LangGraph checkpoint，全部 Task 终态后通过幂等 run.resume 恢复 Root，由 Root 完成最终综合。

