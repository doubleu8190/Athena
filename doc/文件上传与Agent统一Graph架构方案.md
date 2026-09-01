# 文件上传与 Agent 统一 Graph 架构方案

## 1. 文档信息

- **版本：** V1.0
- **状态：** 设计方案
- **适用范围：** 文件上传、文件解析、索引和依赖文件的用户消息处理流程
- **关联文档：** `doc/文件系统设计v2.md`

---

## 2. 背景

当前系统的文件处理流程大致如下：

```text
上传文件
  ↓
文件写入本地存储
  ↓
创建附件记录
  ↓
创建 `message.submit` 命令
  ↓
统一 Graph 解析、分块、索引
  ↓
附件 READY
  ↓
用户消息开始消费附件
```

文件处理、消息持久化和 Agent 执行均由同一个 LangGraph Run 编排。上传请求只保存 Blob、创建附件和命令，后台 `CommandConsumer` 执行 Graph；Graph 在文件处理完成后才进入 Agent 上下文和推理阶段。

---

## 3. 产品约束

本方案基于以下明确约束：

> 当前系统只有“文件随着用户消息一起发送”这一种上传场景。

因此：

- 一次用户消息可以携带一个或多个文件。
- 上传文件属于当前这条用户消息。
- 其它消息如果需要该文件，需要重新上传。
- 不把文件设计成跨消息复用的独立知识库资产。
- 文件解析是当前消息处理流程的前置阶段。

文件虽然在物理存储上仍然是一个独立 Blob，但在业务上属于这次消息提交。

---

## 4. 设计目标

### 4.1 主要目标

1. 将文件上传、文件处理和 Agent 消费组织为同一个业务 Run。
2. 文件未处理完成时，Agent 不开始真正的推理和工具调用。
3. 文件处理完成后，Graph 自动进入 Agent 上下文准备阶段。
4. 使用 LangGraph checkpoint 支持节点级恢复。
5. 通过统一的 Run ID 和 SSE 事件向客户端暴露完整进度。
6. 保留文件内容寻址、解析适配器和多种索引能力。
7. 让文件处理节点具备幂等性，允许 Graph 恢复时安全重试。

### 4.2 非目标

本方案不包含以下内容：

- 将文件内容直接放入 LangGraph State。
- 将大文件二进制保存到 checkpoint。
- 上传请求同步等待解析完成后再返回 HTTP 响应。
- 上传后自动执行所有昂贵的摘要、视觉分析和深度代码分析。
- 为跨消息文件复用建立新的业务模型。

---

## 5. 总体架构

目标架构如下：

```text
┌──────────────┐
│   客户端      │
│ 消息 + 文件   │
└──────┬───────┘
       │ HTTP multipart
       ▼
┌─────────────────────────────┐
│ Gateway                      │
│ - 校验会话                   │
│ - 流式保存 Blob              │
│ - 创建 Attachment            │
│ - 创建 message.submit 命令   │
└──────────────┬──────────────┘
               │ 202 Accepted
               ▼
┌─────────────────────────────┐
│ CommandConsumer              │
│ 后台消费消息命令             │
└──────────────┬──────────────┘
               ▼
┌────────────────────────────────────┐
│ Unified LangGraph Run              │
│                                    │
│ prepare_request                    │
│       ↓                            │
│ persist_message_and_attachments    │
│       ↓                            │
│ process_attachments                │
│       ↓                            │
│ check_file_results                 │
│       ↓                            │
│ prepare_context                    │
│       ↓                            │
│ execute_agent                      │
│       ↓                            │
│ persist_response                   │
└───────┬────────────────────────────┘
        │
        ├──────────────► SQLite
        │                消息、附件、分块、artifact、事件
        │
        ├──────────────► File Storage
        │                原始 Blob、临时工作目录
        │
        └──────────────► ChromaDB
                         向量索引
```

### 5.1 统一 Graph 不等于统一存储

LangGraph 负责：

- 流程编排；
- 节点状态；
- 节点间依赖；
- checkpoint；
- Run 恢复；
- 事件关联。

文件系统、SQLite 和 ChromaDB 仍然负责各自的数据持久化：

- 原始文件字节保存在 `StorageLayer` 管理的 Blob 中；
- 附件和解析结果保存在 SQLite 中；
- 向量索引保存在 ChromaDB 中；
- Graph State 只保存 ID、状态和小型结果。

Graph 不应该成为文件内容数据库。

---

## 6. 请求与后台执行模型

### 6.1 推荐 API

推荐将消息和文件合并为一个 multipart 请求：

```http
POST /api/sessions/{session_id}/runs
Content-Type: multipart/form-data
```

表单字段：

```text
message=请分析这份报告
command_id=cmd-123
files=report.pdf
files=data.xlsx
```

Gateway 的职责是：

1. 校验会话。
2. 校验文件名、扩展名和 MIME 类型。
3. 流式写入内容寻址 Blob。
4. 创建附件记录。
5. 生成本次消息的 `message_id` 和 `run_id`。
6. 创建包含消息、附件 ID、`message_id` 和 `run_id` 的 `message.submit` 命令。
7. 返回 `202 Accepted`。

示例响应：

```json
{
  "command_id": "cmd-123",
  "run_id": "run-456",
  "message_id": "message-789",
  "status": "pending",
  "attachment_ids": ["file-1", "file-2"]
}
```

HTTP 请求不等待文件解析、索引和 Agent 推理完成。

### 6.2 后台运行

后台执行链路如下：

```text
HTTP API
  ↓
写入 command store
  ↓
返回 202
  ↓
CommandConsumer 取出命令
  ↓
invoke_graph(...)
  ↓
Unified LangGraph Run
```

当前 `CommandConsumer` 已经负责消费 `message.submit`，因此统一 Graph 可以复用现有后台入口，不需要让上传接口自行创建 asyncio 后台任务。

复用持久化命令队列的好处是：

- HTTP 请求结束后，Run 仍然可以继续执行；
- 客户端断开不会自动取消后台任务；
- 命令有明确的 `command_id` 和 `run_id`；
- 现有的幂等、会话占用和运行状态机制可以继续使用。

---

## 7. Graph 设计

### 7.1 Graph 节点

建议的节点如下：

```text
START
  ↓
prepare_request
  ↓
persist_message_and_attachments
  ↓
process_attachments
  ↓
check_file_results
  ├── file_failed → handle_file_failure → END
  └── all_ready   → prepare_context
                         ↓
                    retrieve_context
                         ↓
                    execute_agent
                         ↓
                    persist_response
                         ↓
                        END
```

### 7.2 `prepare_request`

职责：

- 验证 `session_id`；
- 去重 `attachment_ids`；
- 检查所有附件属于当前会话；
- 检查附件记录是否存在；
- 加载必要的历史消息；
- 生成规范化的 Graph State。

建议输出：

```python
{
    "session_id": "session-1",
    "run_id": "run-1",
    "command_id": "cmd-1",
    "user_message": "请分析这个文件",
    "attachment_ids": ["file-1"],
    "file_results": [],
}
```

该节点不读取文件正文。

### 7.3 `persist_message_and_attachments`

职责：

- 创建用户消息；
- 将附件绑定到这条用户消息；
- 保存消息和附件关系；
- 在开始文件处理前确定并持久化 `message_id`；
- 保证消息 ID 在 Graph 恢复时不重复创建。

推荐新增一个仓储级事务方法：

```python
await repository.create_message_with_attachments(
    message=message,
    attachment_ids=attachment_ids,
)
```

内部应在同一个 SQLite 事务中完成：

```text
创建 Message
  ↓
校验 Attachment 所属会话
  ↓
写入 MessageAttachment
  ↓
更新 Attachment.message_id
  ↓
提交事务
```

`message_id` 在 Gateway 创建命令时生成并传入 Graph State。文件处理事件必须使用这个稳定的 `message_id`，不能等文件处理完成后再临时生成消息 ID。

如果 Graph 因恢复重复执行该节点，应该通过已有的 `message_id` 和幂等键找到已有消息，而不是再次插入。

### 7.4 `process_attachments`

职责：

- 读取附件元数据；
- 选择适配器；
- 执行解析；
- 生成文本分块；
- 写入分块和全文索引；
- 写入表格、符号和依赖等派生数据；
- 建立向量索引；
- 将附件标记为 `READY` 或 `FAILED`。

单个附件的内部流程：

```text
Attachment UPLOADED
  ↓
选择 Adapter
  ↓
Attachment PROCESSING
  ↓
extract
  ↓
chunk
  ↓
replace_chunks
  ↓
写入 FTS5
  ↓
写入表格/代码索引 artifact
  ↓
写入 ChromaDB
  ↓
Attachment READY
```

现有实现中的 `parse_attachment()` 已经覆盖大部分解析职责，`index_attachment()` 负责向量索引。迁移时应将二者整理成职责明确的 Graph 节点或节点内部服务调用。

### 7.5 文件处理是同步依赖，但不是 HTTP 同步调用

Graph 内部可以使用异步调用：

```python
async def process_attachments(state):
    async def process_one(attachment_id):
        async with parse_semaphore:
            try:
                result = await process_one_attachment(attachment_id)
                return {
                    "message_id": state["message_id"],
                    "attachment_id": attachment_id,
                    "status": "ready",
                    "error": None,
                    "chunk_count": result["chunk_count"],
                }
            except Exception as exc:
                return {
                    "message_id": state["message_id"],
                    "attachment_id": attachment_id,
                    "status": "failed",
                    "error": str(exc),
                    "chunk_count": None,
                }

    results = await asyncio.gather(
        *(process_one(item) for item in state["attachment_ids"])
    )
    return {
        "file_results": results,
        "files_ready": all(item["status"] == "ready" for item in results),
    }
```

这表示：

```text
所有文件处理分支未完成
  ↓
后续 Agent 节点不会执行
```

`parse_semaphore`、向量索引信号量和代码分析信号量分别使用现有配置限制并发。整个 Graph 由后台 `CommandConsumer` 执行，不会阻塞客户端的上传 HTTP 请求。

### 7.6 `check_file_results`

该节点根据所有附件的最终状态路由：

```text
全部 READY
  → prepare_context

任一 FAILED
  → handle_file_failure
```

由于 Graph 会等待 `process_attachments` 返回，正常情况下不会出现 `PENDING` 分支。`PENDING` 只应当用于外部处理器、超时策略或分阶段迁移期间的兼容逻辑。

### 7.7 `prepare_context`

只有文件全部就绪后，才执行：

- 加载历史消息；
- 构建附件引用；
- 生成系统提示词；
- 准备 Harness 输入；
- 让 Agent 知道附件 ID 和文件能力。

文件正文仍然不自动注入上下文。Agent 需要通过正式文件工具访问：

- `read_file`；
- `search_file`；
- `summarize_file`；
- `extract_table`；
- `analyze_file`；
- 代码符号和依赖分析工具。

这保持了“文件不是 Prompt”的设计原则。

---

## 8. Graph State 设计

建议使用显式的 Agent State 扩展文件处理字段：

```python
from typing import Literal, Required, TypedDict


class FileProcessResult(TypedDict):
    message_id: str
    attachment_id: str
    status: Literal["ready", "failed"]
    error: str | None
    chunk_count: int | None


class AgentState(TypedDict, total=False):
    session_id: str
    run_id: str
    command_id: str
    user_message: str
    attachment_ids: list[str]
    message_id: Required[str]
    file_results: list[FileProcessResult]
    files_ready: bool
    file_error: str | None
    history: list[dict]
    harness_messages: list[dict]
```

`file_results` 是 `process_attachments` 节点产生的附件处理结果列表，每个元素对应一个附件：

```python
{
    "message_id": "message-1",
    "attachment_id": "file-1",
    "status": "ready",
    "error": None,
    "chunk_count": 24,
}
```

其中：

- `message_id`：明确该文件处理属于哪条用户消息；
- `attachment_id`：被处理的附件 ID；
- `status`：该附件的最终处理状态；
- `error`：失败原因，成功时为 `None`；
- `chunk_count`：解析后生成的文本分块数量，处理失败时为 `None`。

当一条消息携带多个附件时，列表包含多个结果。`check_file_results` 根据该列表判断是否允许进入 Agent：只有所有结果的 `status` 都是 `ready` 时才继续。

State 中允许保存：

- ID；
- 状态；
- 错误信息；
- 数量统计；
- 小型结构化结果。

State 中禁止保存：

- 文件二进制；
- 整个 PDF 文本；
- 大量 Excel 行数据；
- 完整向量；
- 超大 OCR 图片内容。

这些内容应该通过 `storage_key`、数据库 ID 或 artifact cache key 查询。

---

## 9. 多文件处理策略

### 9.1 Graph 内并行、统一汇聚

```text
             ┌─ 文件 A ─┐
prepare ─────┼─ 文件 B ─┼─ collect_results ─→ Agent
             └─ 文件 C ─┘
```

并行处理仍然满足业务约束：只有所有文件完成后才开始 Agent。

建议使用 Graph 并行分支或节点内部的 `asyncio.gather`，但必须通过配置限制并发：

- PDF/Word/Excel 解析并发受 `file_parse_concurrency` 控制；
- 向量写入受 `file_embedding_concurrency` 控制；
- 摘要和代码分析作为 Agent 工具调用，受统一的工具/运行超时控制。

---

## 10. 状态模型

### 10.1 Run 状态

```text
pending
  ↓
running
  ├─ completed
  ├─ failed
  └─ cancelled
```

### 10.2 附件状态

```text
uploaded
  ↓
processing
  ├─ ready
  └─ failed
```

`deleted` 仍然可以作为管理操作产生的终态，但在统一 Graph 中不应作为正常处理路径。

### 10.3 状态职责

| 状态 | 归属 | 用途 |
| --- | --- | --- |
| Run 状态 | LangGraph/Agent Runtime | 表示整条消息工作流状态 |
| Attachment 状态 | File Repository | 表示文件是否可被访问 |
| Node checkpoint | LangGraph | 表示 Graph 从哪里恢复 |
| Blob 是否存在 | StorageLayer | 表示原始文件是否仍在物理存储中 |

附件状态是真实业务状态，Graph State 不能替代数据库中的附件状态。

---

## 11. 事件与前端观测

统一 Graph 后，客户端仍通过会话 SSE 获取进度。

推荐事件顺序：

```text
run.started
message_persisted(message_id)
attachment_updated(message_id, attachment_id, processing)
file_processing_started(message_id, attachment_id)
file_processing_progress(message_id, attachment_id)
file_index_progress(message_id, attachment_id)
attachment_updated(message_id, attachment_id, ready)
message.started
message.delta
message.completed
run.completed
```

失败示例：

```text
run.started
message_persisted(message_id)
attachment_updated(message_id, attachment_id, processing)
attachment_updated(message_id, attachment_id, failed)
file_processing_failed(message_id, attachment_id)
message.completed 或 run.failed
```

除不属于具体消息的系统级事件外，所有文件相关事件必须包含：

- `session_id`；
- `run_id`；
- `message_id`；
- `attachment_id`；
- 当前状态；
- 进度；
- 错误信息（如有）。

这里的 `message_id` 是文件事件契约中的必填字段，应使用 `ApplicationEvent` 的正式字段暴露，而不是只约定在非标准 payload 中传递。`ApplicationEvent.message_id` 对不属于具体消息的系统事件可以为空；所有由本次消息文件处理节点产生的事件都必须填充该字段。

示例：

```json
{
  "event_type": "attachment_updated",
  "session_id": "session-1",
  "run_id": "run-1",
  "message_id": "message-1",
  "attachment_id": "file-1",
  "status": "processing",
  "progress": 0.35,
  "error_message": null
}
```

当前事件基础设施位于 `athena/gateway/routes/events.py`，文件运行时通过 `emit_attachment()` 和 `emit()` 推送附件及 Graph 文件处理状态。所有事件都应携带 `message_id`，避免客户端只能通过 `run_id` 或 `attachment_id` 猜测关联消息。

---

## 12. 失败、重试和恢复

### 12.1 节点异常

文件处理节点失败时：

1. 捕获底层异常；
2. 将附件标记为 `FAILED`；
3. 保存错误信息；
4. 发布附件失败事件；
5. Graph 路由到 `handle_file_failure`；
6. 不进入 Agent 工具调用和 LLM 推理。

示例：

```python
try:
    result = await process_one_attachment(attachment_id)
except Exception as exc:
    await repository.update_attachment(
        attachment_id,
        status=AttachmentStatus.FAILED,
        error_message=str(exc),
    )
    await runtime.emit_attachment_failed(attachment_id, str(exc))
    raise
```

### 12.2 Graph 恢复

Graph 节点成功完成后由 Checkpointer 保存状态：

```text
persist_message_and_attachments  checkpoint
process_attachment_A             checkpoint
process_attachment_B             checkpoint
check_file_results                checkpoint
prepare_context                   checkpoint
```

如果服务在处理文件 B 前重启，恢复时可以从文件 B 继续。

如果服务在文件 B 的副作用完成、但 checkpoint 尚未写入前重启，文件 B 节点可能被再次执行。因此所有文件处理副作用必须幂等。

### 12.3 用户主动重试

重试恢复原来的消息 Run，从失败的文件处理节点重新执行：

```text
FAILED
  ↓ retry
重新执行 process_attachments
  ↓
成功后继续原 Graph
```

重试必须保留原 `message_id` 和 `attachment_id`，不能因为重新处理文件而创建一条新的用户消息。原有用户消息与附件关系保持不变。

---

## 13. 幂等性要求

统一 Graph 的关键前提是节点可以安全重试。

### 13.1 消息持久化

- 使用稳定的 `message_id` 或 `run_id` 幂等创建。
- 同一个 Run 恢复时不得生成第二条用户消息。
- 附件绑定使用唯一约束或 `on_conflict_do_nothing()`。

### 13.2 分块写入

当前 `replace_chunks()` 的模式适合重复执行：

```text
删除附件旧分块和 FTS 记录
  ↓
重新写入当前解析结果
```

重复执行后，结果仍然应当与执行一次相同。

### 13.3 向量索引

向量索引应使用：

```text
删除当前 attachment_id 的旧向量
  ↓
批量写入新向量
```

不要简单追加，否则 Graph 重试会生成重复向量。

### 13.4 Artifact

摘要、表格等 artifact 使用由文件哈希、能力、参数、适配器版本、模型版本和 Prompt 版本组成的 cache key。

重复执行应当执行 upsert 或复用已有 artifact，而不是创建重复记录。

### 13.5 外部 LLM 调用

LLM 调用通常无法真正回滚。如果一个节点在 LLM 已返回后、checkpoint 前崩溃，恢复可能重复调用。

因此：

- 对昂贵的摘要操作使用 artifact cache key；
- 对可重复的生成操作优先先查缓存；
- 必要时在 artifact 表中记录处理中状态；
- 不把“checkpoint 成功”当作外部调用成功的唯一凭据。

---

## 14. 存储一致性

Blob 文件系统和 SQLite 不是同一个事务，因此无法做到天然的原子提交。

### 14.1 推荐上传顺序

```text
流式写入临时 Blob
  ↓
原子移动到内容寻址路径
  ↓
创建 Attachment 记录
  ↓
创建 message.submit Command
```

如果数据库写入失败，Blob 可能暂时成为孤立文件。

### 14.2 补偿策略

需要保留定期清理机制：

1. 查询所有仍被活动附件引用的 `storage_key`；
2. 扫描 `blobs/` 目录；
3. 删除不在活动引用集合中的 Blob；
4. 对新上传但尚未完成数据库登记的 Blob 增加宽限期。

不能在上传数据库写入失败后立即无条件删除 Blob，因为并发恢复或重试可能仍然需要它。

---

## 15. 与现有组件的关系

### 15.1 保留的组件

- `StorageLayer`：继续管理 Blob、临时工作区和清理；
- `AdapterRegistry`：继续选择 PDF、Word、Excel、图片和代码适配器；
- `FileRepository`：继续管理附件、分块、artifact、代码索引；
- `FileIntelligenceRuntime`：继续提供解析、读取、搜索、摘要和分析能力；
- `CommandConsumer`：继续作为后台消息 Run 的执行入口；
- ChromaDB：继续保存向量索引。

### 15.2 移除的组件

- `FileTaskWorker` 及文件任务调度器；
- `FileProcessingTaskModel`、`processing_tasks`、`file_processing_tasks` 及任务仓储方法；
- `FILE_RETRY`、`FILE_CANCEL` 命令和文件任务 REST 端点；
- `AgentContinuationModel`、`ContinuationRequest` 及所有 continuation 仓储方法。

统一 Graph 完成后不再创建 continuation。应用启动时会删除旧数据库中的
`agent_continuations`、`processing_tasks` 和 `file_processing_tasks` 表。

---

## 16. 统一 Graph 文件处理

统一 Graph 的文件节点直接调用文件 Runtime：

```text
process_attachment
  ├─ parse_attachment
  ├─ persist_chunks_and_derived_data
  ├─ build_vector_index
  └─ mark_ready
```

摘要和代码深度分析不属于上传后的必经路径，继续通过 Agent 工具直接调用 Runtime 按需执行，并使用 artifact 缓存避免重复的昂贵调用。

---

## 17. 正式 API

直接采用统一的 multipart 消息提交 API：

将文件和消息放到同一个请求中：

```text
POST /runs multipart
```

Gateway 负责保存附件并创建命令，客户端只需要维护一次提交语义。

该 API 最准确地表达：

```text
一次消息提交 = 消息内容 + 这次消息携带的文件
```

---

## 18. 迁移计划

迁移从统一 multipart API 开始执行，不保留“独立上传接口 + 后续 attachment_ids 提交”作为新流程。

### 第一步：统一 multipart 提交入口

- 将消息文本和文件放入同一个 multipart 请求；
- Gateway 流式保存 Blob；
- 生成并贯穿整个流程的 `run_id` 和 `message_id`；
- 创建附件记录和 `message.submit` 命令；
- 返回 `202`，不等待解析和 Agent 执行完成。

### 第二步：整理文件处理服务边界

- 将 `parse_attachment()` 的解析、副作用和状态更新整理清楚；
- 将向量索引逻辑从任务类型中解耦；
- 确保 `replace_chunks()`、artifact 和向量索引可重复执行；
- 补充文件状态转换约束。

### 第三步：增加统一 Graph 文件节点

- 增加 `process_attachments` 节点；
- 增加 `check_file_results` 路由；
- 让成功路径直接进入现有 `prepare_context`；
- 让失败路径进入统一错误节点。

### 第四步：将消息和附件保存合并为一个事务

- 增加 `create_message_with_attachments()`；
- 使用稳定 ID 防止 Graph 恢复重复创建；
- 补充消息和附件关系的唯一约束。

### 第五步：移除旧文件任务和 continuation 结构

- 新消息不创建 continuation；
- 文件未完成时由 Graph 节点直接等待；
- 删除 FileTaskWorker 及其启动、停止和 Runtime 注入逻辑；
- 删除文件任务命令、REST 端点、仓储方法和 ORM 模型；
- 删除 `AgentContinuationModel`、`ContinuationRequest` 和相关仓储方法；
- 启动时删除旧数据库中的 `agent_continuations`、`processing_tasks` 和 `file_processing_tasks` 表。

---

## 19. 风险与控制措施

| 风险 | 影响 | 控制措施 |
| --- | --- | --- |
| 文件处理耗时过长 | Run 长时间处于 running | 节点级 timeout、进度事件、后台执行 |
| Graph 恢复重复执行 | 重复分块或向量 | 所有文件副作用幂等 |
| LLM 调用重复 | 成本增加 | artifact cache、稳定 cache key |
| Blob 与 DB 不一致 | 孤立文件 | 宽限期清理和定期补偿 |
| 多文件耗时过长 | 用户等待时间增加 | 初期串行，稳定后并行汇聚 |
| 服务重启 | Run 中断 | Checkpointer + 节点恢复 |
| 用户取消 | 解析仍继续运行 | 节点检查取消信号，底层操作支持超时 |
| 过多内容进入 State | checkpoint 膨胀 | State 只保存 ID 和小型结果 |

---

## 20. 验收标准

### 功能验收

- 提交无附件消息时，Graph 直接进入 Agent 流程；
- 提交一个附件时，文件处理完成后才开始 Agent 推理；
- 提交多个附件时，全部成功后才开始 Agent 推理；
- 任一附件失败时，不调用 Agent 文件工具和 LLM 推理；
- 文件处理成功后，Agent 可以通过 `file_id` 使用文件工具；
- 文件解析失败后可以从文件处理节点重试；
- 客户端断开后，后台 Run 仍可继续执行。

### 恢复验收

- 文件解析节点执行期间进程重启后，Run 可以恢复；
- 已完成节点不会重复创建用户消息；
- 重复执行解析不会产生重复分块、重复 FTS 记录或重复向量；
- 已生成 artifact 可以被缓存复用。

### 观测验收

- 客户端可以通过 SSE 看到 Run 和附件状态变化；
- 文件失败时事件包含明确的附件 ID 和错误信息；
- Graph 完成后可以按 `run_id` 查询完整执行记录。

---

## 21. 结论

在当前“文件随消息上传，且只属于这条消息”的产品约束下，文件解析不必再被建模为独立于用户消息的等待任务。

推荐的最终模型是：

```text
一次消息提交
  = 上传文件
  + 创建附件
  + 文件解析和索引
  + Agent 推理
  + 保存回复
```

这些阶段由同一个 LangGraph Run 串联，但 Run 由后台 `CommandConsumer` 执行，HTTP 接口只负责接受请求并返回 `202`。

核心原则是：

> 业务流程同步等待文件完成，执行载体异步运行；Graph State 保存流程状态，文件系统和数据库保存文件数据。

该方案能够消除当前文件 Worker 与 Agent continuation 之间的协调复杂度，同时保留内容寻址存储、文件适配器、全文检索、向量检索、事件推送和服务重启恢复能力。
