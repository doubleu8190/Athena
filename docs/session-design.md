# Session 设计

**状态：** Session CRUD 已实施。本项目前支持可选项目绑定、标题维护、软删除
和列表筛选；消息发送 HTTP 接口不在本轮范围内。消息写入仍由内部
`MessageService` 提供给后续 Run 流程。

## 1. 目标与边界

Session 是用户连续对话及其项目上下文的容器。一个 Session：

- 有且只有一个经过服务端认证的 owner。
- 可以绑定一个项目，也可以不绑定项目。
- 有可选标题；未命名 Session 使用 `title = null`。
- 删除采用软删除，不物理删除消息或执行轨迹。

本轮不实现：

- 消息发送 HTTP 接口。
- 项目共享成员或 Session 转让。
- Session 的硬删除、恢复和用户删除后的保留策略。
- 跨进程、跨实例的全局唯一 ID。

## 2. 归属规则

### 2.1 Session owner

Session 的 `owner_id` 只能来自 `CurrentAuth.user.id`。创建、查询、改名、改绑、
解绑和删除都不能接受请求体、query 或客户端请求头指定的 owner。

不存在、已软删除或属于其他用户的 Session 统一返回：

```json
{"detail": "session_not_found"}
```

HTTP 状态为 `404`，避免暴露跨用户资源是否存在。

### 2.2 项目绑定

- Session 创建时可以直接绑定项目。
- Session 可以在没有消息时创建，之后在首条消息前绑定、改绑或解绑。
- 绑定只接受当前 owner 名下、未软删除的项目。其他用户的项目和不存在项目
  都返回 `404 project_not_found`。
- 数据库用复合外键
  `(sessions.project_id, sessions.owner_id) -> (projects.id, projects.owner_id)`
  再次保证 Session owner 严格等于 Project owner。
- 暂不支持项目共享成员，因此不存在“Session owner 在项目成员中”这一分支。

### 2.3 首条消息后的绑定不可变

消息写入会推进 `sessions.last_message_sequence` 并更新 `updated_at`。当
`last_message_sequence > 0` 时：

- 修改 `project_id`，包括从项目 A 改到项目 B，返回
  `409 session_project_locked`。
- 显式设置 `project_id: null` 解绑，同样返回 `409 session_project_locked`。
- 标题仍可修改。
- 重新提交与当前值相同的 `project_id` 不视为修改，可以和其他字段一起保存。

`SessionService.update` 先通过 `get_visible` 确认 Session 可见性，以保证
不可见资源优先返回 `404 session_not_found`，且不会因标题或目标项目的字段
错误泄漏更多状态。该预检不承担并发判断；字段规范化后仍调用
`SessionRepository.update`，仓储在同一写事务内先用 `FOR UPDATE` 锁定
Session 行，再重新判断归属、软删除状态和 `last_message_sequence`。消息追加
也锁定同一 Session 行，因此首条消息提交与项目绑定修改严格串行。

## 3. 项目删除约束

项目仍存在最少一个 `deleted_time IS NULL` 的绑定 Session 时，软删除返回：

```json
{"detail": "project_has_sessions"}
```

HTTP 状态为 `409`。用户需要先删除这些 Session，或在没有消息时将它们解绑。

保护不只停留在先查后删：

- Session 创建或改绑项目时，在同一数据库事务内对目标项目行取得共享锁，
  并再次确认项目属于当前 owner 且未删除。
- 项目软删除在同一事务内对项目行取得更新锁，再查询有效绑定 Session。
- 因此并发的“创建/改绑 Session”和“删除项目”会被串行化；任一顺序都不会
  留下“项目已删除但仍有有效绑定 Session”的状态。

项目删除是软删除，不级联修改 Session 或消息。存在有效绑定 Session 时要求
先处理会话，避免隐式改绑或丢数据。

## 4. 数据模型

`sessions` 字段：

| 字段 | 说明 |
| --- | --- |
| `id` | Session 主键，最大 64 字符 |
| `owner_id` | 用户 ID，指向 `users.id` |
| `title` | 可选标题，最大 120 字符 |
| `project_id` | 可选项目 ID，指向 `projects.id` |
| `last_message_sequence` | 会话流和执行流共享的消息序号 |
| `created_at` | 创建时间 |
| `updated_at` | 最近修改时间，列表排序依据 |
| `deleted_time` | 软删除时间 |

数据库约束：

- `last_message_sequence >= 0`。
- `title IS NULL OR length(title) <= 120`。
- `(project_id, owner_id)` 复合外键指向 `(projects.id, projects.owner_id)`。
- 活动列表索引：
  `(owner_id, updated_at DESC, id DESC) WHERE deleted_time IS NULL`。
- 活动项目列表索引：
  `(owner_id, project_id, updated_at DESC, id DESC) WHERE deleted_time IS NULL`。

迁移：

```text
20261105_0005 add session catalog fields and ownership constraints
```

## 5. 应用与 HTTP 契约

### 5.1 创建

```text
POST /api/v1/sessions

{
  "title": "可选标题",
  "project_id": "可选项目 ID"
}
```

- `title` 省略或为 `null`：无标题。
- `project_id` 省略或为 `null`：未绑定项目。
- 成功返回 `201`，并设置 `Location: /api/v1/sessions/{session_id}`。
- 请求体禁止额外字段，包括 `owner_id` 和 `deleted_time`。

### 5.2 列表

```text
GET /api/v1/sessions?page=1&page_size=20
GET /api/v1/sessions?project_id=<project_id>
GET /api/v1/sessions?unbound=true
```

- `page >= 1`，`page_size` 为 `1..100`。
- `project_id` 与 `unbound=true` 不能同时使用，否则返回
  `422 invalid_session_filter`。
- 只返回当前 owner 的未删除 Session，按 `updated_at DESC, id DESC` 排序。
- 项目筛选不额外检查项目是否存在；不存在或不可见的项目只会得到空列表，
  不泄漏项目归属。

### 5.3 查询、修改与删除

```text
GET    /api/v1/sessions/{session_id}
PATCH  /api/v1/sessions/{session_id}
DELETE /api/v1/sessions/{session_id}
```

PATCH 只接受 `title` 和 `project_id`，并按客户端是否显式提交字段判断更新：

- `{"title": "新标题"}`：改名，去除首尾空白。
- `{"title": null}`：清空标题。
- `{"project_id": "<project_id>"}`：绑定或改绑。
- `{"project_id": null}`：解绑。
- `{}`：返回 `422 session_update_invalid`。

标题校验失败返回 `422 session_title_invalid`。绑定不存在的项目返回
`404 project_not_found`；首条消息后的绑定变更返回
`409 session_project_locked`。DELETE 成功返回 `204`，同一 owner 重复删除
仍返回 `204`。

PATCH 先做可见性预检，再校验标题和目标项目格式，随后调用仓储更新事务。
不可见 Session 立即返回 `404 session_not_found`，不会因为标题或目标项目
不合法而泄漏更多状态。仓储事务重新锁定 Session 行并确认其属于当前 owner
且未删除；预检不替代行锁。确认 Session 可见后才检查目标项目并在同一事务内
完成更新。

更新是字段级 no-op：只有标题或 `project_id` 实际变化时才写库并推进
`updated_at`。重新提交与当前值相同的标题或项目绑定仍返回 `200`，即使 Session
已经有首条消息也不触发 `session_project_locked`，且不会改变列表排序。

公开 Session 响应固定为：

```json
{
  "id": "...",
  "title": null,
  "project_id": null,
  "created_at": "...",
  "updated_at": "..."
}
```

`owner_id`、`deleted_time` 和内部 `last_message_sequence` 不出现在响应中。

## 6. 并发与事务边界

Repository 的数据库行锁是唯一并发事实源，不维护额外的进程内锁、通用锁端口或
数据库外锁：

- Session 标题/项目绑定更新先锁定 Session 行 `FOR UPDATE`。只有该行属于
  当前 owner 且未删除时才继续；已有消息时拒绝改绑或解绑。
- 所有推进 Session `updated_at` 的写入都在持有 Session 行锁后取当前值与
  候选时间的较大值。后取得锁的请求不能把较晚提交的时间覆盖回更早值，消息
  自身的时间字段不受影响。
- Session 软删除也锁定 Session 行 `FOR UPDATE`。同一 owner 重复删除返回
  成功，其他 owner 和无对应行按不可见处理。
- 三类内部消息追加都在各自写事务内对相同 Session 行执行 `FOR UPDATE`，再
  完成幂等查询、引用校验、共享 sequence 分配和插入。消息写入与 Session
  删除、绑定修改不会穿越。
- Session 创建绑定项目以及改绑项目时，在同一事务内对目标 Project 行执行
  `FOR SHARE`，并重新确认 owner、存在性和软删除状态。
- Project 软删除对 Project 行执行 `FOR UPDATE`，在锁内检查是否仍有未删除
  绑定 Session。Session 创建/改绑与项目删除因此按行锁串行化，不会留下
  “项目已删除但仍有有效绑定 Session”的状态。
- 事务提交、回滚、业务异常和任务取消都由 PostgreSQL 自动释放行锁，因此
  约束跨连接、跨进程成立。无需自行处理锁释放或连接清理。

当前锁顺序简单且不存在 Project 更新锁后再锁 Session 行的反向路径：Session
改绑是“Session 行 -> Project 共享行”，项目删除是“Project 行 -> 查询绑定
Session”。消息追加只锁 Session 行。

该方案要求被保护资源在数据库中已有行。没有数据库行的资源不能通过本方案取得
互斥；若未来出现这种真实需求，应针对具体资源单独设计，不预建通用锁抽象。

`Database` 只暴露一套有界业务连接池。等待行锁的 SQL 会继续占用其业务连接，
因此 `ATHENA_DATABASE_POOL_SIZE` 与 `ATHENA_DATABASE_MAX_OVERFLOW` 需要覆盖
正常查询和并发锁等待，并受 PostgreSQL 连接数上限约束。

## 7. 组装与测试

- `domain/session`：实体、分页对象、业务异常和仓储端口。
- `app/session`：标题与分页校验、用例编排和结果模型；不做项目预读或加锁。
- `adapter/session`：SQLAlchemy ORM、纯转换、事务内 CRUD 和 Session 行锁。
- `adapter/message`：在消息写入事务内锁定同一个 Session 行。
- `adapter/project`：在项目删除事务内锁定 Project 行并检查绑定 Session。
- `api`：请求 DTO、响应 DTO、错误映射和 Cookie 身份依赖。
- `create_app` 在数据库模式组装 SessionService、ProjectService、MessageService
  和仓储，不注入额外锁对象。

自动测试覆盖：

- 创建有项目/无项目 Session、标题规范化与非法标题。
- owner 过滤及跨用户项目绑定拒绝。
- 列表排序、项目筛选、未绑定筛选和非法筛选组合。
- 首条消息前改绑/解绑，首条消息后的绑定锁定和标题修改。
- 不可见 Session 的 PATCH 错误优先级，以及相同标题/项目绑定的 no-op 时间戳
  和列表排序。
- 重复删除、软删除不可见和项目占用删除冲突。
- 未认证、跨 origin 写请求和数据库故障隐藏。
- SQLite 仓储测试与 PostgreSQL 临时 schema 迁移/仓储测试。
- 真实 PostgreSQL 事件交错测试：首条消息持 Session 行锁时改绑阻塞，消息
  提交后改绑得到 `session_project_locked`。
- 真实 PostgreSQL 时间单调性测试：消息先以较晚时间提交并持锁时，较早取时
  的标题修改等待；消息提交后标题修改成功但 `updated_at` 保持较晚时间。
- Session 创建持 Project 共享行锁时项目删除阻塞；创建提交后删除得到
  `project_has_sessions`。反向顺序下项目先软删除，等待中的创建得到
  `project_not_found`。
- Session 改绑持目标 Project 共享行锁时项目删除阻塞，改绑提交后删除得到
  `project_has_sessions`。反向顺序下项目先软删除，等待中的改绑得到
  `project_not_found`，不会留下活动绑定。
- Session 删除与消息写入双向交错：删除先提交时消息得到
  `session_not_found`；消息先提交时删除随后成功，且已删除 Session 不再接受
  新消息。

## 8. 已知边界

- Session 使用 `sess_<uuid4hex>`；项目仍使用进程内单调 UTC 毫秒 ID。
- 只有一套业务连接池；高并发下同一 Session 的行锁等待会占用连接，容量规划
  必须同时考虑并发请求和等待中的事务。
- 行锁只能保护数据库已有行。没有持久化行的资源不在当前并发语义覆盖范围内。
- 项目共享成员、Session 转让和用户删除后的资源保留策略尚未设计。
- 消息发送接口和 Run 编排尚未实现；本轮只保证已存在内部消息写入后绑定
  锁定语义生效。
