# 消息设计

**状态：** Phase 1 已实施。覆盖稳定消息存储、只读会话历史、Session CRUD、
基于 Repository 的数据库行锁和迁移；Run 写入编排、流式增量尚未实现。
认证首版已接入应用工厂，消息读取和内部写入使用请求级或调用方已验证的用户
身份。

## 1. 边界

消息是用户对话和 Agent 执行共同使用的持久化事实，但两类事实的生命周期和
消费方不同，因此拆成两条流：

| 消息流 | 角色 | 消费方 | 是否公开 |
| --- | --- | --- | --- |
| `conversation_messages` | user、最终 assistant | 聊天历史、后续对话上下文、记忆提取 | 只读历史接口 |
| `execution_messages` | system、user、中间 assistant、tool | Agent 调用轨迹、工具关联、审计与恢复 | 不公开 |

工具调用和工具结果只进入执行流。Subagent 或 Worker 的内部内容不能写入会话
流，否则会污染主对话和后续模型上下文。

## 2. 内容模型

消息正文是类型化内容块 `parts`，不使用无约束的裸文本或任意字典：

- `text`：文本。
- `file_ref`：文件模块引用，保存文件 ID、名称、MIME 类型和可选大小。
- `tool_call`：工具调用 ID、工具名和参数。
- `tool_result`：工具结果状态、文本预览或大型结果引用。

不保存供应商隐藏推理、Chain-of-Thought、凭证或原始 provider 请求体。当前
Phase 1 不实现 artifact 文件存储，超大工具结果只能保存安全预览和
`artifact_id` 引用。

## 3. 数据表

### 3.1 sessions

消息依赖的最小 Session 表：

- `id`
- `owner_id`
- `title`
- `project_id`
- `last_message_sequence`
- `created_at`
- `updated_at`
- `deleted_time`

Session CRUD 已实现，支持可选项目绑定、标题和软删除。消息读取与写入必须
同时匹配未删除 Session 和 `owner_id`；不存在、已删除和 owner 不匹配统一按
`session_not_found` 处理。

`domain/session` 定义 `SessionScope`、`Session` 和 `SessionRepository`；
`adapter/session/{model,converter,repository}` 保存 ORM、纯转换和按 owner
过滤的 CRUD，并在更新、软删除时锁定 Session 行。消息历史仍不先做独立可见性
查询，而是在同一条
SQL 中 LEFT JOIN Session 表并应用同样条件，避免两次读取之间发生软删除或
owner 变化造成消息泄漏。

### 3.2 conversation_messages

- `id`
- `session_id`
- `role`：`user` 或 `assistant`
- `status`：`pending / streaming / completed / failed / cancelled`
- `sequence`：Session 内唯一、单调递增
- `parts`
- `reply_to_message_id`
- `run_id`
- `client_message_id`
- `created_at`
- `completed_at`
- `metadata`

user 消息的 `(session_id, client_message_id)` 在非空时唯一；同一 Run 的
conversation 角色唯一，避免重复完成事务写入两条最终 assistant 消息。
assistant 的 `reply_to_message_id` 必须引用同一 Session 的 user 消息；
不存在、跨 Session 或指向 assistant 都会被拒绝。

### 3.3 execution_messages

- `id`
- `session_id`
- `run_id`
- `thread_id`
- `sequence`
- `role`：`system / user / assistant / tool`
- `parts`
- `tool_call_id`
- `source_message_id`
- `created_at`
- `metadata`

`tool` 消息必须恰好包含一个 `tool_result`，且消息级 `tool_call_id` 与内容块
一致。写入时必须能在同一 `session_id + run_id + thread_id` 的 assistant
execution message 中找到对应 `tool_call`；同范围跨 assistant 消息的
`tool_call_id` 也不得重复。`(session_id, run_id, thread_id, tool_call_id)`
在 `tool_call_id` 非空时唯一，保证恢复或重放不会重复追加工具结果，同时允许
不同 Session 或 Run 复用相同线程名和工具调用 ID。

`source_message_id` 只允许引用同一 Session 的 canonical conversation message
（user 或最终 assistant），不能引用 execution message。数据库使用外键约束，
应用层同时检查 Session 和角色，避免只依赖外键错误。

### 3.4 adapter 分层

适配器按业务领域分包，不把所有 SQLAlchemy 模型和仓储平铺在 `adapter` 根：

- `adapter/message/model/`：`ConversationMessageModel`、
  `ExecutionMessageModel`；Session 作为 Phase 1 的独立最小模型位于
  `adapter/session/model/`。
- `adapter/message/converter/`：parts 与 metadata JSON 序列化、反序列化和
  ORM 行到领域对象的纯转换。
- `adapter/message/repository/`：事务、查询、幂等与引用校验以及持久化编排，
  不内嵌大段转换逻辑。
- `adapter/session/model|converter|repository/`：Session ORM、领域转换、
  所有者范围内的 CRUD 和数据库行锁。消息读取为保持同一 SQL 快照会直接 JOIN
  `SessionModel`，但不复制 Session ORM 定义。
- `adapter/auth/`：`users`、`auth_sessions` 的 ORM、转换和认证仓储。
- `adapter/base.py`：共享 Declarative Base。
- `adapter/models.py`：聚合导入 auth、project、message、session 的全部 ORM
  模型，供 Alembic 和测试注册 metadata，不定义业务表。

`adapter/project` 采用相同的 `model / converter / repository` 结构。消息 API
路径、数据库表名和迁移 revision 不因 adapter 分包而变化。

## 4. 顺序与生命周期

- 同一 Session 的会话流和执行流共用一个 `last_message_sequence` 计数器。
- `MessageService` 的三个内部写入方法必须接收已认证 `owner_id`，并直接调用
  仓储写事务；应用服务不再持有或获取额外锁。
- 写入事务开始时按 `session_id + owner_id + deleted_time IS NULL` 锁定
  Session 行，在锁内完成幂等查询、引用校验、sequence 分配和插入；同一
  Session 串行写入，不同 Session 可以并发。
- Session 标题/绑定修改、Session 删除与消息写入锁定同一个 Session 行，因此
  首条消息和“绑定已锁定”判断不会互相穿越。Session 创建/改绑项目时还会对
  Project 行取得 `FOR SHARE`；项目删除对 Project 行取得 `FOR UPDATE` 并检查
  未删除绑定 Session。锁随事务提交、回滚、异常或取消自动释放。
- 数据库只有一套有界业务连接池。行锁等待会占用业务连接，池容量需要同时覆盖
  正常查询和并发等待，并受 PostgreSQL 总连接数限制。
- 不使用时间戳排序，也不把任意 `IntegrityError` 重查后当作幂等成功。
- user 消息写入时即为 `completed`；assistant 的 Phase 1 内部写入只接受
  `completed / failed / cancelled` 终态。
- 完成消息不修改正文。当前未实现 token 增量、assistant 状态机或 SSE
  重放；这些属于 Run/流式设计。
- 消息写入只通过内部 `MessageService`，未来由 Run 提交流程调用。HTTP 不提供
  消息写接口。

## 5. 只读接口

```text
GET /api/v1/sessions/{session_id}/messages?before_sequence=&limit=20
```

- 只返回 `conversation_messages`。
- `limit` 为 `1..100`。
- 查询按 `sequence` 倒序取一页，响应页内按 `sequence` 升序。
- `next_before_sequence` 非空时作为下一页的 `before_sequence`。
- 不返回内部 `metadata` 或任何 execution message。

消息路由在应用提供 `MessageService` 时始终挂载。请求必须携带有效的
`athena_session` HttpOnly Cookie；`CurrentAuth` 从服务端认证会话取得当前
用户，缺失、过期、撤销或无效 Cookie 统一返回
`401 {"detail":"not_authenticated"}`。应用不读取客户端用户 ID 请求头，
也没有固定 principal 回退。

消息查询把 `CurrentAuth.user.id` 作为 owner，与 Session 可见性在同一个 SQL
快照中筛选。Session 不存在、已删除或 owner 不匹配统一返回
`404 session_not_found`。PostgreSQL 迁移 `20261105_0004` 已让
`sessions.owner_id` 指向真实 `users.id`，因此 Session 写入必须来自已持久化
用户。

## 6. 迁移与测试

Alembic 迁移：

```text
20261105_0001 create projects
20261105_0002 create sessions and message streams
20261105_0003 add project owner
20261105_0004 create users, auth sessions, and owner foreign keys
20261105_0005 add session catalog fields and ownership constraints
```

测试覆盖：

- 两条流的角色隔离。
- Session 级共享 sequence。
- user client message 幂等和冲突。
- assistant per-run 唯一。
- tool call/result 关联与重复结果。
- 内容块 JSON 往返。
- owner 过滤、分页和数据库错误隐藏。
- assistant 回复目标、execution 来源和 tool call/result 关联校验。
- 拒绝路径不插入消息、不推进 Session sequence。
- 内部写入 owner 与 Session owner 不匹配时拒绝且不推进 sequence。
- SQLite 仓储测试与 PostgreSQL 临时 schema 集成测试。
- 真实 PostgreSQL 并发交错：消息先锁定 Session 行时，Session 改绑或删除
  必须阻塞；消息提交后改绑得到 `session_project_locked`，删除随后成功。
- 删除先提交时并发消息得到 `session_not_found`，不得写入新消息或推进
  `last_message_sequence`。
- 已删除 Session 不再接受任何消息追加。

## 7. 后续事项

1. Run 提交事务内写入 user 消息，完成事务内写入 assistant 消息。
2. assistant streaming 状态机与 SSE delta，不把 token 增量写入消息表。
3. 删除用户时 Session/消息保留策略。
4. 文件/artifact 存储、结果截断策略和数据保留/删除策略。
5. 历史上下文压缩、记忆提取和分支/重新生成语义。
