# LangGraph + SQLite + SSE 流式事件推送技术方案

## 1. 背景

在 Agent 系统中，通常同时存在两类需求：

1. **实时性需求**
   - 用户希望通过 SSE 实时看到 Agent 的执行过程。
   - 包括 LLM 流式输出、Tool 执行状态、节点执行状态等。

2. **可靠性需求**
   - 服务重启后需要恢复任务状态。
   - SSE 客户端断线后需要支持事件恢复。
   - Agent 执行过程需要支持审计、排错和问题定位。
   - LangGraph State 需要通过 Checkpoint 持久化。

如果简单地将所有 SSE Event 直接写入 SQLite，例如 LLM 每生成一个 Token 就插入一条数据库记录，会导致严重的高频写入问题：

```text
LLM Token
   ↓
INSERT SQLite
   ↓
LLM Token
   ↓
INSERT SQLite
   ↓
LLM Token
   ↓
INSERT SQLite
```

这种模式会导致：

- SQLite 高频事务提交。
- WAL 文件频繁增长。
- 多 Agent 并发时 Writer 竞争增加。
- 数据库存储大量价值较低的短生命周期 Token。
- SSE 事件粒度过细。
- SQLite 写入成为整个 Agent Runtime 的性能瓶颈。

因此，本方案的核心原则是：

> **实时事件和持久化事件采用不同的存储策略。SQLite 用于可靠状态和关键事件持久化，不作为高频 Token Streaming Buffer。**

---

# 2. 总体设计目标

本方案采用：

```text
LangGraph
    +
SQLite Checkpoint
    +
SQLite Durable Event
    +
In-Memory Realtime Event
    +
SSE
```

实现以下能力：

- LLM Token 实时推送。
- Tool 执行过程实时推送。
- SQLite 避免高频 Token 写入。
- 关键 Agent 生命周期事件可靠持久化。
- SSE 客户端支持断线恢复。
- Agent 崩溃后能够恢复 State。
- 支持多个 Agent 和多个 Tool 并发执行。
- 与 LangGraph Checkpoint 机制协同工作。

---

# 3. 核心设计原则

整个系统将数据分为三类：

```text
┌─────────────────────────────────────────┐
│            Agent Runtime                │
└───────────────────┬─────────────────────┘
                    │
        ┌───────────┼────────────┐
        │           │            │
        ▼           ▼            ▼
   Realtime      Batch       Durable
    Event        Event        Event
        │           │            │
        ▼           ▼            ▼
     Memory      Buffer       SQLite
        │           │            │
        └───────────┼────────────┘
                    │
                    ▼
                   SSE
```

三种事件分别承担不同职责。

---

## 3.1 Realtime Event

Realtime Event 用于高频、短生命周期的实时数据。

典型事件：

```text
message_delta
reasoning_delta
token
progress_delta
```

例如：

```json
{
  "type": "message_delta",
  "data": {
    "delta": "你好，"
  }
}
```

这类事件的特点：

- 高频。
- 数据量小。
- 生命周期短。
- 主要用于 UI 实时展示。
- 不一定需要长期保存。

因此：

> **Realtime Event 默认只存在于内存中，不直接写入 SQLite。**

数据流：

```text
LLM
 │
 │ Token
 ▼
RealtimeHub
 │
 ▼
In-Memory Channel
 │
 ▼
SSE Client
```

---

## 3.2 Batch Event

Batch Event 用于：

- 需要一定程度恢复能力。
- 但是写入频率较高。
- 不适合每次变化立即持久化。

典型数据：

```text
stream_snapshot
partial_output
progress_snapshot
```

例如：

```json
{
  "task_id": "task-001",
  "content": "用户问题正在分析中，目前已经完成知识库检索。",
  "version": 12
}
```

Batch Event 不按照每个 Token 写入，而采用：

```text
Time Window
     或
Token Count
     或
Buffer Size
```

进行批量 Flush。

推荐默认策略：

```text
500ms
或
4KB
或
50 Tokens
```

任意条件满足即可 Flush。

数据流：

```text
LLM Token
   │
   ▼
StreamBuffer
   │
   ├──────> RealtimeHub ──────> SSE
   │
   │
   │ 每 500ms / 4KB / 50 Tokens
   ▼
SQLite Stream Snapshot
```

---

## 3.3 Durable Event

Durable Event 是系统的重要生命周期事件。

例如：

```text
task_started
task_completed
task_failed

node_started
node_completed
node_failed

tool_started
tool_completed
tool_failed

checkpoint_created
```

这类事件具有以下特点：

- 事件频率较低。
- 对任务恢复有价值。
- 对审计有价值。
- 对问题排查有价值。
- 需要可靠持久化。

因此：

> **Durable Event 必须写入 SQLite。**

例如：

```json
{
  "task_id": "task-001",
  "event_type": "tool_completed",
  "data": {
    "tool_name": "search",
    "status": "success"
  }
}
```

---

# 4. 系统总体架构

推荐的整体架构如下：

```text
┌──────────────────────────────────────────────┐
│                 Agent Runtime                │
│                                              │
│                 LangGraph                    │
│                                              │
│     Node          Tool             LLM       │
└──────┬─────────────┬───────────────┬─────────┘
       │             │               │
       │             │               │
       ▼             ▼               ▼
 Durable Event   Durable Event   Stream Delta
       │             │               │
       └─────────────┼───────────────┘
                     │
                     ▼
              EventPublisher
                     │
       ┌─────────────┴──────────────┐
       │                            │
       ▼                            ▼
DurableEventWriter             RealtimeHub
       │                            │
       ▼                            ▼
SQLite Write Queue         Memory Channel
       │                            │
       ▼                            ▼
SQLite Database                 SSE Client
```

其中：

### LangGraph

负责：

- Agent 工作流执行。
- State 管理。
- Node 执行。
- Tool 调用。
- Checkpoint 持久化。

### EventPublisher

负责：

- 统一接收 Agent Runtime Event。
- 判断 Event 类型。
- 根据 Event Durability 路由。

### DurableEventWriter

负责：

- SQLite 写入。
- Event Batch。
- 单 Writer 控制。
- Event ID 分配。

### RealtimeHub

负责：

- 管理 Task 的实时 Channel。
- Token Buffer。
- SSE Subscriber。
- 实时事件广播。

---

# 5. SQLite 数据职责

SQLite 在本架构中主要承担四类数据持久化。

---

## 5.1 Task

保存任务基本信息。

例如：

```sql
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

Task 状态例如：

```text
PENDING
RUNNING
COMPLETED
FAILED
CANCELLED
```

---

## 5.2 LangGraph Checkpoint

Checkpoint 由 LangGraph Checkpointer 管理。

Checkpoint 用于：

```text
State Persistence
+
Workflow Recovery
+
Execution Resume
```

应用层不应该重复维护 LangGraph State。

职责划分如下：

```text
LangGraph State
       │
       ▼
LangGraph Checkpointer
       │
       ▼
SQLite
```

应用层主要保存：

```text
Task Metadata
Durable Event
Stream Snapshot
Final Output
```

---

## 5.3 Durable Event

建议单独建立 Event 表。

```sql
CREATE TABLE task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
```

建议建立索引：

```sql
CREATE INDEX idx_task_events_task_id_event_id
ON task_events(task_id, event_id);
```

这样可以支持：

```text
Task Event Replay
```

例如：

```sql
SELECT *
FROM task_events
WHERE task_id = ?
AND event_id > ?
ORDER BY event_id ASC;
```

用于 SSE Resume。

---

## 5.4 Stream Snapshot

Stream Snapshot 用于保存流式输出的最近状态。

例如：

```sql
CREATE TABLE task_stream_snapshots (
    task_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,

    PRIMARY KEY (task_id, stream_id)
);
```

例如：

```text
task_id: task-001
stream_id: assistant-message-001

content:

你好，我正在为你查询相关信息，目前已经找到三个相关结果。

version: 12
```

Stream Snapshot 不保存每一个 Token，而保存当前累积结果。

---

# 6. Realtime Stream 设计

LLM 的 Token Streaming 不直接写入 SQLite。

数据流：

```text
LLM
 │
 │ token
 ▼
StreamBuffer
 │
 ├───────────────► SSE
 │
 │
 ▼
Snapshot Buffer
```

例如：

```python
class StreamBuffer:

    def __init__(self):
        self.content = []
        self.pending_delta = []
```

收到 Token：

```python
async def append(token: str):
    self.content.append(token)
    self.pending_delta.append(token)
```

Realtime SSE：

```python
delta = "".join(pending_delta)

await realtime_hub.publish(
    task_id=task_id,
    event_type="message_delta",
    data={
        "delta": delta
    }
)
```

客户端收到：

```text
event: message_delta
data: {"delta":"你好，我正在"}
```

---

# 7. Token Coalescing

不建议：

```text
1 Token
=
1 SSE Event
```

建议使用 Token Coalescing。

例如原始 Token：

```text
你
好
，
我
正
在
查
询
```

Buffer：

```text
你好，我正在查询
```

然后发送一个 SSE Event：

```text
event: message_delta
data: {"delta":"你好，我正在查询"}
```

推荐 Flush 条件：

```text
50ms
或
512 Bytes
```

例如：

```python
class StreamCoalescer:

    def __init__(
        self,
        flush_interval_ms=50,
        max_buffer_size=512,
    ):
        self.buffer = []
```

这样可以降低：

- SSE Event 数量。
- JSON 序列化次数。
- 网络系统调用次数。
- 前端渲染次数。

---

# 8. Stream Snapshot Flush 策略

虽然 Token 不直接写 SQLite，但需要定期保存 Snapshot。

推荐策略：

```text
Flush if:

500ms
OR
50 Tokens
OR
4KB
```

例如：

```python
if (
    elapsed >= 0.5
    or token_count >= 50
    or buffer_size >= 4096
):
    await save_snapshot()
```

保存：

```sql
INSERT INTO task_stream_snapshots (
    task_id,
    stream_id,
    content,
    version,
    updated_at
)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(task_id, stream_id)
DO UPDATE SET
    content = excluded.content,
    version = excluded.version,
    updated_at = excluded.updated_at;
```

这种方式使得：

```text
1000 Tokens
```

可能只产生：

```text
10 ~ 30 次 SQLite 写入
```

而不是：

```text
1000 次 SQLite INSERT
```

---

# 9. Durable Event 写入模型

SQLite 推荐使用：

```text
Single Writer
+
Write Queue
+
Batch Transaction
```

架构：

```text
Agent A ──────┐
Agent B ──────┤
Agent C ──────┤
Tool X ───────┤
Tool Y ───────┤
              ▼
       Durable Event Queue
              │
              ▼
         Event Writer
              │
              ▼
         SQLite Transaction
```

原因是 SQLite 在 WAL 模式下虽然支持多个 Reader，但 Writer 仍然存在串行写入特性。

因此避免：

```text
多个 Agent
    ↓
同时 INSERT SQLite
```

而是：

```text
多个 Producer
    ↓
统一 Write Queue
    ↓
Single Writer
    ↓
SQLite
```

---

# 10. Event Writer Batch 策略

EventWriter 可以采用：

```text
Max Batch Size
+
Max Wait Time
```

例如：

```text
100 Events
或
50ms
```

任意条件满足即提交。

伪代码：

```python
class DurableEventWriter:

    def __init__(self):
        self.queue = asyncio.Queue()

    async def run(self):

        while True:

            batch = []

            event = await self.queue.get()

            batch.append(event)

            deadline = now() + 0.05

            while len(batch) < 100:

                try:
                    event = self.queue.get_nowait()
                    batch.append(event)

                except asyncio.QueueEmpty:

                    if now() >= deadline:
                        break

                    await asyncio.sleep(0.001)

            await self.write_batch(batch)
```

数据库：

```python
async def write_batch(events):

    async with transaction():

        await executemany(
            """
            INSERT INTO task_events
            (
                task_id,
                event_type,
                payload,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            events,
        )
```

这样：

```text
1000 Events

错误方式：
1000 INSERT
1000 COMMIT

推荐方式：
10 Transaction
每个 Transaction 100 Events
```

---

# 11. SSE Resume 机制

建议 Durable Event 使用：

```text
event_id
```

作为 SSE ID。

例如：

```text
id: 100
event: tool_started
data: {...}
```

客户端断线：

```text
Client Disconnect
```

浏览器重新连接：

```http
Last-Event-ID: 100
```

服务端查询：

```sql
SELECT *
FROM task_events
WHERE task_id = ?
AND event_id > ?
ORDER BY event_id ASC;
```

然后重放：

```text
101
102
103
104
```

---

# 12. Streaming Event 的 Resume

Streaming Event 不需要保存每个 Token。

断线恢复流程：

```text
Client Disconnect
       │
       ▼
Last Snapshot
       │
       ▼
SQLite
       │
       ▼
Client Reconnect
       │
       ▼
Load Snapshot
       │
       ▼
Resume Realtime Stream
```

例如数据库中：

```text
Snapshot Version: 12

Content:

你好，我正在查询相关信息，目前已经找到
```

客户端重新连接后：

```text
event: stream_snapshot

data:
{
    "version": 12,
    "content": "你好，我正在查询相关信息，目前已经找到"
}
```

然后继续接收：

```text
event: message_delta
data: {"delta":"三个相关结果。"}
```

---

# 13. EventPublisher 统一接口

建议系统内部只暴露统一的 EventPublisher。

```python
class EventPublisher:

    async def publish(
        self,
        task_id: str,
        event_type: str,
        data: dict,
        durability: str,
    ):
        ...
```

例如：

## Realtime Event

```python
await publisher.publish(
    task_id=task_id,
    event_type="message_delta",
    data={
        "delta": content
    },
    durability="realtime",
)
```

路径：

```text
EventPublisher
       │
       ▼
RealtimeHub
       │
       ▼
SSE
```

---

## Batch Event

```python
await publisher.publish(
    task_id=task_id,
    event_type="stream_snapshot",
    data={
        "content": content
    },
    durability="batch",
)
```

路径：

```text
EventPublisher
       │
       ▼
Snapshot Buffer
       │
       ▼
Batch Flush
       │
       ▼
SQLite
```

---

## Durable Event

```python
await publisher.publish(
    task_id=task_id,
    event_type="tool_completed",
    data={
        "tool_name": "search",
        "status": "success"
    },
    durability="durable",
)
```

路径：

```text
EventPublisher
       │
       ▼
Durable Event Queue
       │
       ▼
Single Writer
       │
       ▼
SQLite
```

---

# 14. Event Durability 定义

建议定义：

```python
from enum import Enum


class EventDurability(str, Enum):

    REALTIME = "realtime"

    BATCH = "batch"

    DURABLE = "durable"
```

Event 类型分类：

| Event | Durability |
|---|---|
| message_delta | REALTIME |
| reasoning_delta | REALTIME |
| token | REALTIME |
| progress_delta | REALTIME |
| stream_snapshot | BATCH |
| partial_output | BATCH |
| task_started | DURABLE |
| task_completed | DURABLE |
| task_failed | DURABLE |
| node_started | DURABLE |
| node_completed | DURABLE |
| node_failed | DURABLE |
| tool_started | DURABLE |
| tool_completed | DURABLE |
| tool_failed | DURABLE |
| checkpoint_created | DURABLE |

---

# 15. Agent 执行完整数据流

一个 Agent Task 的完整执行过程如下：

```text
User Request
      │
      ▼
Create Task
      │
      ▼
LangGraph Execute
      │
      ├──────────────┐
      │              │
      ▼              ▼
Durable Event      LLM Stream
      │              │
      ▼              ▼
Event Writer     Stream Buffer
      │              │
      ▼              ├──────────────► SSE
SQLite             │
                   │
                   ▼
              Snapshot Flush
                   │
                   ▼
                 SQLite
```

最终：

```text
Task Completed
      │
      ▼
Durable Event
      │
      ▼
SQLite
      │
      ▼
SSE
```

---

# 16. 与 LangGraph Checkpoint 的职责边界

必须明确：

```text
LangGraph Checkpoint
```

和：

```text
SSE Event Persistence
```

不是同一件事情。

职责如下：

| 数据 | 负责组件 |
|---|---|
| Agent State | LangGraph Checkpointer |
| Workflow Resume | LangGraph Checkpointer |
| Node Execution State | LangGraph |
| Task Metadata | Application |
| SSE Durable Event | Event Store |
| Realtime Token | RealtimeHub |
| Stream Snapshot | Application |

因此：

```text
LangGraph State
       │
       ▼
Checkpoint
       │
       ▼
SQLite
```

和：

```text
SSE Event
       │
       ├── Realtime ──► Memory
       │
       ├── Batch ─────► SQLite Snapshot
       │
       └── Durable ───► SQLite Event Log
```

两条链路保持独立。

---

# 17. 最终推荐配置

针对单机部署或中等并发 Agent 场景，推荐：

## SQLite

```text
Journal Mode: WAL
```

保存：

```text
Task
LangGraph Checkpoint
Durable Event
Stream Snapshot
Final Output
```

---

## Realtime

```text
In-Memory Event Channel
```

保存：

```text
Token Buffer
SSE Subscriber
Realtime Event
```

---

## Stream Coalescing

推荐：

```text
50ms
OR
512 Bytes
```

发送一次 SSE。

---

## Stream Snapshot

推荐：

```text
500ms
OR
50 Tokens
OR
4KB
```

写入一次 SQLite。

---

## Durable Event

推荐：

```text
Single Writer
+
Async Queue
+
Batch Transaction
```

Batch：

```text
100 Events
OR
50ms
```

---

# 18. 最终结论

本方案的核心设计是：

> **SQLite 负责可靠性，Memory 负责实时性。**

具体来说：

```text
Realtime Event
    ↓
Memory
    ↓
SSE


Batch Event
    ↓
Buffer
    ↓
SQLite Snapshot


Durable Event
    ↓
Write Queue
    ↓
Single Writer
    ↓
SQLite
```

对于 LLM Streaming：

```text
Token
    ↓
不要直接写 SQLite
```

而采用：

```text
Token
    ↓
Memory Buffer
    ↓
SSE

同时：

Token
    ↓
Periodic Snapshot
    ↓
SQLite
```

这种架构可以同时获得：

- 实时 Token Streaming。
- 较低的 SQLite 写入压力。
- SSE 断线恢复能力。
- Agent 执行过程审计能力。
- LangGraph State 恢复能力。
- 多 Agent 并发执行能力。

最终形成：

> **LangGraph 负责 Agent State，SQLite 负责可靠持久化，Memory 负责实时事件，SSE 负责向客户端推送。**

这是当前 Agent Runtime 中较适合 SQLite 的事件推送架构。