# 基于 SQLite Event Store + LangGraph Checkpoint 的 Agent SSE Endpoint 事件推送机制设计

## 1. 背景

Agent 系统通常存在以下执行过程：

- Agent 执行开始
- LLM 调用
- LLM Token 流式输出
- Tool 调用
- Tool 并发执行
- Tool Progress
- Human Approval
- Agent Interrupt
- Agent Resume
- Task Completed
- Task Failed

前端需要实时感知 Agent 的执行状态，例如：

```text
Agent Started

↓

LLM Thinking

↓

Tool Started

↓

Tool Progress 30%

↓

Tool Completed

↓

LLM Generating Response

↓

Task Completed
```

SSE（Server-Sent Events）适合作为：

> Server → Frontend 的实时事件推送通道。

同时，Agent 执行过程需要支持：

- 中断
- 审批
- 恢复
- 系统重启恢复
- Tool 并发
- Agent State 持久化

这些能力由 LangGraph Checkpoint 机制负责。

因此，本方案采用：

```text
SSE Event
    ↓
SQLite Event Store

Agent State
    ↓
LangGraph Checkpointer
```

两个机制职责明确分离。

---

# 2. 架构核心原则

整个系统存在两套不同的持久化数据：

```text
Event
```

表示：

> 发生了什么。

例如：

```text
Tool Started

Tool Progress 30%

Tool Completed
```

而：

```text
Checkpoint
```

表示：

> Agent 当前执行到了哪里，以及恢复执行需要什么 State。

例如：

```json
{
    "messages": [...],

    "current_step": "analyze_file",

    "pending_approval": {
        "tool": "delete_file"
    }
}
```

因此：

```text
┌────────────────────┐
│     Agent Event    │
│                    │
│  Execution History │
└─────────┬──────────┘
          │
          ▼
   SQLite Event Store


┌────────────────────┐
│ Agent Checkpoint   │
│                    │
│ Recovery State     │
└─────────┬──────────┘
          │
          ▼
 LangGraph Checkpointer
```

核心原则：

> SQLite Event Store 不负责 Agent State 恢复。

> LangGraph Checkpointer 不负责 SSE Event Replay。

---

# 3. 为什么不再手动保存 Checkpoint

在手动实现 Agent Runtime 时，通常需要：

```text
Agent Runtime
      │
      ├── Update State
      │
      ├── Save Checkpoint
      │
      ├── Update Task
      │
      └── Publish Event
```

需要自己处理：

- Checkpoint Version
- State Snapshot
- Checkpoint Recovery
- Crash Recovery
- Resume Point
- Concurrent State Update

使用 LangGraph 后：

```text
Agent Runtime
      │
      ▼
LangGraph Graph
      │
      ▼
State Transition
      │
      ▼
LangGraph Checkpointer
```

Checkpoint 生命周期由 LangGraph 管理。

因此业务层不再直接：

```python
checkpoint_store.save(...)
```

或者：

```python
checkpoint_store.load(...)
```

而是通过 LangGraph：

```python
graph.invoke(
    input,
    config
)
```

或者：

```python
graph.stream(
    input,
    config
)
```

让 LangGraph 根据：

```text
thread_id
```

自动定位对应执行状态。

---

# 4. 整体架构

修改后的架构：

```text
                           Frontend
                              │
                ┌─────────────┴─────────────┐
                │                           │
                │ HTTP                      │ SSE
                ▼                           ▲
            Task API                    SSE Endpoint
                │                           │
                ▼                           │
          Agent Runtime                      │
                │                           │
                ▼                           │
           LangGraph Graph                    │
                │                           │
        ┌───────┴────────┐                   │
        │                │                   │
        ▼                ▼                   │
LangGraph Checkpointer   Event Publisher      │
        │                │                   │
        ▼                ▼                   │
   Agent State       SQLite Event Store      │
                            │                │
                            └────────────────┘
```

职责：

### LangGraph

负责：

```text
Agent State

State Transition

Checkpoint

Interrupt

Resume
```

### SQLite

负责：

```text
Task Metadata

Agent Event

Tool Event

SSE Replay
```

### SSE Endpoint

负责：

```text
读取 Event

推送 Event

Event Replay
```

---

# 5. Task 与 LangGraph Thread 的关系

建议：

```text
Task ID = LangGraph Thread ID
```

例如：

```text
task_id:

task_123
```

LangGraph：

```python
config = {
    "configurable": {
        "thread_id": "task_123"
    }
}
```

关系：

```text
agent_tasks
     │
     │ task_id
     ▼
LangGraph thread_id
```

这样：

```text
Task
```

就是：

> 一个 Agent Execution Thread。

系统重启后：

```text
Frontend
   │
   ▼
task_id
   │
   ▼
thread_id
   │
   ▼
LangGraph Checkpointer
   │
   ▼
Load Checkpoint
```

无需业务层自己寻找：

```text
checkpoint_id
```

---

# 6. SQLite Task 表

即使 LangGraph 负责 Checkpoint，仍然建议保存 Task Metadata。

例如：

```sql
CREATE TABLE agent_tasks (

    task_id TEXT PRIMARY KEY,

    status TEXT NOT NULL,

    created_at DATETIME NOT NULL,

    updated_at DATETIME NOT NULL
);
```

Task 表不再保存：

```text
current_checkpoint_id

checkpoint_version

agent_state
```

这些由：

```text
LangGraph Checkpointer
```

负责。

Task 表只保存业务状态，例如：

```text
CREATED

RUNNING

WAITING_APPROVAL

COMPLETED

FAILED

CANCELLED
```

---

# 7. SQLite Event 表

Agent Event 继续使用 SQLite 保存。

```sql
CREATE TABLE agent_events (

    event_id INTEGER PRIMARY KEY AUTOINCREMENT,

    task_id TEXT NOT NULL,

    event_type TEXT NOT NULL,

    data TEXT NOT NULL,

    created_at DATETIME NOT NULL
);
```

索引：

```sql
CREATE INDEX idx_agent_events_task_event
ON agent_events (
    task_id,
    event_id
);
```

其中：

```text
event_id
```

作为：

> SSE Cursor。

---

# 8. Event 模型

```python
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AgentEvent(BaseModel):

    event_id: int | None = None

    task_id: str

    event_type: str

    data: dict[str, Any]

    created_at: datetime | None = None
```

例如：

```python
AgentEvent(
    task_id="task_123",

    event_type="tool.progress",

    data={
        "tool_execution_id": "tool_001",
        "progress": 30,
        "message": "正在解析 PDF"
    }
)
```

---

# 9. Event 类型

建议统一定义：

```text
task.created

task.started

task.progress

task.waiting_approval

task.resumed

task.completed

task.failed

task.cancelled


agent.started

agent.step.started

agent.step.completed


llm.started

llm.delta

llm.completed


tool.started

tool.progress

tool.completed

tool.failed


tool.approval.required
```

---

# 10. Event Publisher

Agent Graph 不应该直接依赖：

```text
SSE Connection
```

Graph 只需要：

```text
Publish Event
```

例如：

```python
class EventPublisher:

    def __init__(
        self,
        event_store
    ):
        self.event_store = event_store


    async def publish(
        self,
        task_id: str,
        event_type: str,
        data: dict,
    ):

        return await self.event_store.append(
            task_id=task_id,
            event_type=event_type,
            data=data,
        )
```

Graph Node：

```python
async def analyze_node(
    state,
    config,
):

    task_id = (
        config["configurable"]
        ["thread_id"]
    )

    await event_publisher.publish(
        task_id,
        "agent.step.started",
        {
            "step": "analyze"
        }
    )

    result = await analyze(...)

    await event_publisher.publish(
        task_id,
        "agent.step.completed",
        {
            "step": "analyze"
        }
    )

    return {
        "analysis": result
    }
```

---

# 11. LangGraph Checkpointer 配置

Graph 编译时注入 Checkpointer：

```python
graph = builder.compile(
    checkpointer=checkpointer
)
```

调用：

```python
config = {
    "configurable": {
        "thread_id": task_id
    }
}
```

执行：

```python
result = await graph.ainvoke(
    input,
    config=config
)
```

LangGraph 根据：

```text
thread_id
```

管理：

```text
Checkpoint

State

Execution Progress
```

因此业务代码无需：

```python
save_checkpoint(...)
```

---

# 12. Task 创建流程

```text
POST /tasks
      │
      ▼
Create Task Metadata
      │
      ▼
SQLite
      │
      ▼
Create thread_id
      │
      ▼
Start LangGraph
      │
      ▼
LangGraph Checkpointer
      │
      ▼
Agent Running
```

示例：

```python
@app.post("/tasks")
async def create_task():

    task_id = str(
        uuid.uuid4()
    )

    await task_repository.create(
        task_id=task_id,
        status="CREATED"
    )

    await event_store.append(
        task_id,
        "task.created",
        {}
    )

    asyncio.create_task(
        run_agent(task_id)
    )

    return {
        "task_id": task_id
    }
```

执行：

```python
async def run_agent(
    task_id: str
):

    config = {
        "configurable": {
            "thread_id": task_id
        }
    }

    await task_repository.update_status(
        task_id,
        "RUNNING"
    )

    await event_store.append(
        task_id,
        "task.started",
        {}
    )

    try:

        await graph.ainvoke(
            {},
            config=config
        )

        await task_repository.update_status(
            task_id,
            "COMPLETED"
        )

        await event_store.append(
            task_id,
            "task.completed",
            {}
        )

    except Exception as e:

        await task_repository.update_status(
            task_id,
            "FAILED"
        )

        await event_store.append(
            task_id,
            "task.failed",
            {
                "error": str(e)
            }
        )
```

---

# 13. SSE Endpoint

SSE Endpoint 不需要理解：

```text
LangGraph State

Checkpoint

Graph Node
```

只读取：

```text
agent_events
```

例如：

```text
GET /tasks/{task_id}/events
```

---

## FastAPI 示例

```python
import asyncio

from fastapi import Header
from sse_starlette.sse import EventSourceResponse


@app.get("/tasks/{task_id}/events")
async def task_events(

    task_id: str,

    last_event_id: str | None = Header(
        default=None,
        alias="Last-Event-ID"
    ),
):

    async def event_generator():

        cursor = (
            int(last_event_id)
            if last_event_id
            else 0
        )

        while True:

            events = (
                await event_store.fetch_after(
                    task_id,
                    cursor,
                )
            )

            for event in events:

                cursor = event.event_id

                yield {
                    "id": str(event.event_id),

                    "event": event.event_type,

                    "data": event.data,
                }

                if event.event_type in {
                    "task.completed",
                    "task.failed",
                    "task.cancelled",
                }:
                    return

            await asyncio.sleep(0.3)

    return EventSourceResponse(
        event_generator()
    )
```

---

# 14. SSE Event Replay

Event：

```text
Event 101
Event 102
Event 103
```

Frontend 收到：

```text
101
102
```

然后：

```text
Disconnect
```

浏览器记录：

```text
Last-Event-ID: 102
```

重新连接：

```text
GET /tasks/task_123/events
```

服务端：

```sql
SELECT *
FROM agent_events
WHERE
    task_id = ?
    AND event_id > 102
ORDER BY event_id ASC;
```

返回：

```text
Event 103
```

因此：

```text
SSE Disconnect
      │
      ▼
Agent Continue
      │
      ▼
Event Persisted
      │
      ▼
Reconnect
      │
      ▼
Replay Missing Events
```

---

# 15. Human Approval 与 LangGraph Interrupt

Approval 是 LangGraph Checkpoint 非常适合处理的场景。

Graph：

```text
Agent
   │
   ▼
Need Tool Approval
   │
   ▼
LangGraph Interrupt
   │
   ▼
LangGraph Save Checkpoint
   │
   ▼
WAITING_APPROVAL
```

Node：

```python
from langgraph.types import interrupt


def dangerous_tool_node(
    state,
    config,
):

    approval = interrupt(
        {
            "type": "tool_approval",

            "tool_name": "delete_file",

            "arguments": {
                "file_id": "file_123"
            }
        }
    )

    return approval
```

同时发布 SSE Event：

```text
tool.approval.required
```

推荐流程：

```text
Graph Node
      │
      ▼
Publish Approval Event
      │
      ▼
interrupt()
      │
      ▼
LangGraph Checkpoint
      │
      ▼
Graph Paused
```

前端：

```text
SSE

tool.approval.required
```

显示：

```text
Approve / Reject
```

---

# 16. Approval API

Approval 不应该通过 SSE 返回。

SSE：

```text
Server
   ↓
Client
```

Approval：

```text
Client
   ↓
Server
```

因此使用 HTTP：

```text
POST /tasks/{task_id}/resume
```

例如：

```json
{
    "decision": "approve"
}
```

后端：

```python
from langgraph.types import Command


@app.post(
    "/tasks/{task_id}/resume"
)
async def resume_task(
    task_id: str,
    decision: str,
):

    config = {
        "configurable": {
            "thread_id": task_id
        }
    }

    await event_store.append(
        task_id,
        "task.resumed",
        {
            "decision": decision
        }
    )

    result = await graph.ainvoke(
        Command(
            resume={
                "decision": decision
            }
        ),
        config=config,
    )

    return {
        "status": "ok"
    }
```

LangGraph：

```text
thread_id
    │
    ▼
Load Checkpoint
    │
    ▼
Resume Graph
```

无需：

```python
load_checkpoint(...)
```

---

# 17. Tool 并发与 LangGraph Checkpoint

使用 LangGraph 后仍然需要注意：

> Tool 可以并发，但不要让多个并发 Tool 任意覆盖同一个 State 字段。

例如：

```text
             Parallel Tools
                   │
       ┌───────────┼───────────┐
       ▼           ▼           ▼
     Tool A      Tool B      Tool C
```

推荐：

```text
Tool A
    ↓
Result A

Tool B
    ↓
Result B

Tool C
    ↓
Result C
```

最终由：

```text
LangGraph State Merge
```

处理。

例如：

```python
from typing import Annotated

from langgraph.graph.message import add_messages


class AgentState(TypedDict):

    messages: Annotated[
        list,
        add_messages
    ]

    tool_results: dict
```

如果多个节点同时更新：

```python
{
    "tool_results": {
        "A": result_a
    }
}
```

和：

```python
{
    "tool_results": {
        "B": result_b
    }
}
```

不要简单依赖：

```text
last write wins
```

建议定义：

```text
Reducer
```

或者采用：

```text
独立 Tool Execution Storage
```

---

# 18. 推荐的并发 Tool 模型

即使使用 LangGraph：

```text
Agent State
```

仍然应该保持：

> Agent State 是编排状态。

Tool Execution 是：

> 执行状态。

因此：

```text
Agent
   │
   ▼
LangGraph
   │
   ├── Tool A
   │
   ├── Tool B
   │
   └── Tool C
```

每个 Tool 可以发布：

```text
tool.started

tool.progress

tool.completed

tool.failed
```

这些 Event：

```text
SQLite Event Store
```

而 LangGraph：

```text
Checkpoint
```

保存：

```text
当前 Graph Execution State
```

---

# 19. 系统重启后的恢复

这是 LangGraph Checkpointer 的主要价值。

系统崩溃：

```text
Graph Running
      │
      ▼
Node Completed
      │
      ▼
LangGraph Checkpoint Saved
      │
      ▼
System Crash
```

系统重启：

```text
Agent Runtime
      │
      ▼
Task ID
      │
      ▼
thread_id
      │
      ▼
LangGraph Checkpointer
      │
      ▼
Latest Checkpoint
      │
      ▼
Resume
```

业务层不需要：

```text
Deserialize Agent State

Restore Current Step

Restore Checkpoint Version
```

这些由：

```text
LangGraph
```

完成。

但是需要注意：

> LangGraph Checkpointer 保存的是 Graph State，不等于自动重新启动所有中断的业务任务。

因此系统启动后仍然需要：

```text
Recovery Manager
```

扫描：

```text
agent_tasks
```

例如：

```text
RUNNING
```

状态的任务。

然后判断：

```text
是否需要重新调用 Graph
```

例如：

```python
async def recover_tasks():

    tasks = (
        await task_repository.list_by_status(
            "RUNNING"
        )
    )

    for task in tasks:

        asyncio.create_task(
            run_agent(
                task.task_id
            )
        )
```

LangGraph：

```text
根据 thread_id

恢复对应 State
```

---

# 20. Task Metadata 与 LangGraph Checkpoint 的职责

推荐：

| 数据 | 存储位置 |
|---|---|
| Task ID | SQLite |
| Task Status | SQLite |
| Task Created Time | SQLite |
| Task Updated Time | SQLite |
| Agent Execution Event | SQLite |
| SSE Cursor | SQLite Event ID |
| Agent State | LangGraph Checkpointer |
| Graph Checkpoint | LangGraph Checkpointer |
| Interrupt State | LangGraph Checkpointer |
| Resume State | LangGraph Checkpointer |

即：

```text
SQLite
    │
    ├── Task Metadata
    │
    └── Event History


LangGraph Checkpointer
    │
    └── Agent Execution State
```

---

# 21. Event 与 LangGraph Stream 的关系

LangGraph 本身可以：

```python
graph.astream_events(...)
```

获取执行事件。

但不建议：

```text
LangGraph Stream
      │
      ▼
直接连接 Frontend
```

推荐：

```text
LangGraph Stream
      │
      ▼
Event Adapter
      │
      ▼
SQLite Event Store
      │
      ▼
SSE
      │
      ▼
Frontend
```

例如：

```python
async for event in graph.astream_events(
    input,
    config=config,
):

    normalized_event = (
        event_adapter.normalize(
            event
        )
    )

    if normalized_event:

        await event_store.append(
            task_id,
            normalized_event.type,
            normalized_event.data,
        )
```

这样可以将：

```text
LangGraph Internal Event
```

转换为：

```text
Application Event
```

避免前端直接依赖 LangGraph 内部事件格式。

---

# 22. 推荐的运行流程

完整流程：

```text
POST /tasks
      │
      ▼
Create Task
      │
      ▼
SQLite Task Metadata
      │
      ▼
task.created Event
      │
      ▼
Start LangGraph
      │
      ▼
LangGraph Checkpointer
      │
      ▼
Graph Running
      │
      ├── Agent Event
      │
      ├── LLM Event
      │
      ├── Tool Event
      │
      └── Approval Event
               │
               ▼
        SQLite Event Store
               │
               ▼
         SSE Endpoint
               │
               ▼
            Frontend
```

---

# 23. 推荐项目结构

```text
app/

├── api/
│
│   ├── tasks.py
│   ├── approvals.py
│   └── events.py
│
├── graph/
│
│   ├── builder.py
│   ├── state.py
│   ├── nodes/
│   └── tools/
│
├── runtime/
│
│   ├── task_runner.py
│   └── recovery_manager.py
│
├── event/
│
│   ├── models.py
│   ├── publisher.py
│   ├── event_store.py
│   └── sqlite_event_store.py
│
├── storage/
│
│   ├── sqlite.py
│   └── task_repository.py
│
├── langgraph/
│
│   └── checkpointer.py
│
└── main.py
```

---

# 24. 最终架构

```text
                           Frontend
                              │
                    ┌─────────┴─────────┐
                    │                   │
                  HTTP                  SSE
                    │                   ▲
                    ▼                   │
                API Layer               │
                    │                   │
                    ▼                   │
                Task Runner             │
                    │                   │
                    ▼                   │
              LangGraph Graph           │
                    │                   │
          ┌─────────┴──────────┐        │
          │                    │        │
          ▼                    ▼        │
     Checkpointer        Event Publisher│
          │                    │        │
          ▼                    ▼        │
   Agent Execution        SQLite Event  │
       State                  Store     │
                                  │     │
                                  └─────┘
```

---

# 25. 最终核心原则

## 原则一：LangGraph 负责 Checkpoint

业务代码不再：

```text
Save Checkpoint

Load Checkpoint

Manage Version
```

而是：

```text
thread_id
```

交给：

```text
LangGraph Checkpointer
```

---

## 原则二：SQLite 负责 Event

SQLite 是：

```text
SSE Event Source of Truth
```

负责：

```text
Persist

Replay

Reconnect
```

---

## 原则三：Event 与 State 分离

```text
Event

发生了什么
```

与：

```text
Checkpoint

当前应该如何继续
```

是两个不同的概念。

---

## 原则四：SSE 不直接依赖 LangGraph

推荐：

```text
LangGraph
    │
    ▼
Application Event
    │
    ▼
SQLite
    │
    ▼
SSE
```

而不是：

```text
LangGraph
    │
    ▼
SSE
    │
    ▼
Frontend
```

---

## 原则五：Task ID = Thread ID

推荐：

```text
task_id
    =
thread_id
```

这样：

```text
Task

Event

SSE

Checkpoint
```

全部围绕一个统一 ID：

```text
task_id
```

---

# 26. 总结

最终方案：

```text
                     Agent Task
                         │
                         ▼
                   task_id
                         │
            ┌────────────┴────────────┐
            │                         │
            ▼                         ▼

      LangGraph Thread          SQLite Event Store

            │                         │
            ▼                         ▼

      LangGraph Checkpoint       Agent Events

            │                         │
            │                         ▼
            │                    SSE Endpoint
            │                         │
            │                         ▼
            │                     Frontend
            │
            ▼
       Crash Recovery
       Interrupt / Resume
```

最终职责划分：

```text
LangGraph
    ↓
Agent State / Checkpoint / Interrupt / Resume

SQLite
    ↓
Task Metadata / Event History

SSE
    ↓
Realtime Push / Replay

Frontend
    ↓
Render Agent Execution Process
```

因此整个系统不再维护自定义：

```text
Checkpoint Store

Checkpoint Version

Checkpoint Recovery Logic
```

而是：

> **使用 LangGraph Checkpointer 管理 Agent 的执行状态，使用 SQLite Event Store 管理面向前端的持久化事件流。**

这样可以显著降低 Agent Runtime 的复杂度，同时保留：

- Agent State 持久化
- Interrupt / Resume
- Human Approval
- 系统重启恢复
- SSE 实时推送
- SSE Event Replay
- Tool Progress
- Tool 并发
- Event History

并且：

> **Checkpoint 是 LangGraph 的内部执行机制，Event 是系统对外的可观测性协议。**

这两个机制分离后，整个 Agent Runtime 的边界会更加清晰。