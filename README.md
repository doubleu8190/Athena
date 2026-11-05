# Athena v2

面向多用户的通用智能助手。

已确认的产品范围：直接回答、搜索阅读与分析、文件生成、MCP、Skill、
自定义业务 Subagent、知识库和记忆系统。

当前已实现邮箱密码认证、项目目录 CRUD、Session 目录 CRUD，以及消息存储和
只读会话历史。认证使用服务端可撤销登录会话和 HttpOnly Cookie，项目、Session
和消息 API 都从已验证的请求级登录上下文确定当前用户。Run 编排、消息发送
接口、流式输出和其余产品能力尚未实现。

项目管理支持：

- 创建项目、修改项目名、软删除项目和查询项目。
- 每个项目都有必填的内部 `owner_id`，由服务端从已验证的登录 Cookie 写入；
  请求体、query 和客户端自报身份都不能指定所有者。HTTP 响应不暴露
  `owner_id`。
- 列表、详情、改名和删除均按当前登录用户过滤；其他用户的项目一律按
  不存在处理并返回 `404`。同一用户重复软删除仍返回 `204`。
- 项目仍存在未删除的绑定 Session 时，删除返回 `409 project_has_sessions`；
  必须先把这些 Session 解绑或删除。
- 项目主键使用进程内单调 UTC 毫秒时间 ID，由
  `common.ids.monotonic_timestamp_id_factory()` 提供。
- 项目名去除首尾空白后必须为 1–120 个字符，不允许控制字符；允许重名。
- 列表按最近修改时间倒序分页，软删除项目不会出现在列表或详情中。
- 迁移 `20261105_0004` 创建 `users`、`auth_sessions` 和 owner 外键；
  `20261105_0005` 增加 Session 标题、项目绑定、复合所有者外键和活动列表索引。
- 认证、项目、Session 和消息路由在服务存在时始终挂载；未登录、Cookie 缺失、
  会话过期或已被撤销时统一返回 `401 {"detail":"not_authenticated"}`，不会
  回退到服务端固定 principal 或客户端自报用户 ID。

当前项目 ID 生成器只在单个进程内维护单调序列，不提供跨进程、跨重启或
多实例部署的唯一性保证。横向扩容或高可靠部署前必须替换为集群级唯一 ID
方案；不能把现生成器描述为跨进程单调或全局唯一。

会话支持：

- 创建、查询、改名、改绑/解绑项目和软删除 Session；标题可为空，去除首尾
  空白后必须为 1–120 个字符，且不允许控制字符。
- Session 可以绑定项目，也可以保持未绑定。绑定项目只接受同一 owner 且未
  删除的项目；其他用户的项目统一按 `404 project_not_found` 处理。
- Session 出现首条消息后，`project_id` 不可再改绑或解绑，返回
  `409 session_project_locked`；标题仍可修改。消息事务推进
  内部 `last_message_sequence` 时更新 `updated_at`，列表按最近修改时间倒序。
- Session 删除采用软删除，同一 owner 重复删除保持幂等；跨 owner 的查询、
  修改和删除统一返回 `404 session_not_found`。
- PATCH 直接进入仓储更新事务；事务先锁定 Session 行并确认其属于当前 owner
  且未删除，再检查目标项目；不可见 Session 始终优先返回
  `404 session_not_found`。
- PATCH 只有字段实际变化时才写库并更新 `updated_at`。相同标题或相同
  `project_id` 是成功 no-op，即使已有首条消息也不会触发绑定锁定或让会话上浮。
- Session 列表支持 `project_id`、`unbound`、`page` 和 `page_size` 筛选；
  同时传 `project_id` 与 `unbound=true` 返回 `422 invalid_session_filter`。
- 项目删除会在事务内锁定项目行并检查未删除绑定 Session；Session 创建或改绑
  会在同一写事务内对目标项目取得共享行锁，避免并发时绕过“项目仍被占用时
  不可删除”的约束。
- Session 主键使用 `sess_<uuid4hex>`；ID 工厂可注入，测试可使用确定性实现。
- 并发控制只依赖 PostgreSQL 事务内的 Repository 行锁，不维护额外锁对象、
  进程内锁或数据库外锁。Session 更新、软删除与消息追加使用同一 Session
  行 `FOR UPDATE`；Session 创建或改绑使用 Project 行 `FOR SHARE`；项目删除
  使用 Project 行 `FOR UPDATE` 并检查未删除绑定 Session。事务提交、回滚、
  异常和取消都会由 PostgreSQL 自动释放行锁，因此约束跨进程生效。
- 该方案以数据库已有行为锁对象；当前不支持锁定没有数据库行的资源。若未来
  出现此类需求，应针对实际资源单独设计，不预建通用锁抽象。
- 本轮只提供 Session CRUD，不提供消息发送 HTTP 接口；消息写入仍只由内部
  `MessageService` 提供给未来的 Run 流程。

消息支持：

- `conversation_messages` 保存 user 和最终 assistant 消息，供聊天历史和后续
  对话上下文使用。
- `execution_messages` 保存 system、user、中间 assistant 和 tool 消息，供
  Agent 执行轨迹使用；不通过公开接口返回。
- 内容使用类型化 parts，支持文本、文件引用、工具调用和工具结果。
- 同一 Session 的 conversation 与 execution 消息共用单调 sequence。
- 写入在 Session 行锁内完成幂等检查、引用校验和 sequence 分配；assistant
  回复目标、execution 来源及 tool result 的调用关联都会在写入事务内校验。
- 适配器按领域分包：消息 ORM、纯转换和仓储分别位于
  `adapter/message/{model,converter,repository}`，仓储只负责事务、查询和
  持久化编排。
- 消息写入只由内部应用服务提供，未来由 Run 提交流程调用；当前没有消息写
  HTTP 接口。
- 内部消息追加必须携带已认证的 `owner_id`，并在同一事务的 Session 行锁内
  匹配 Session owner；
  跨用户或不可见 Session 统一按 `session_not_found` 拒绝。
- Session 目录由 `domain/session` 的实体与端口、`app/session` 用例和
  `adapter/session/{model,converter,repository}` 组成。消息历史在读取消息的
  同一条 SQL 中 JOIN Session 并筛选 owner 和未删除状态，避免先查可见性再
  另开事务读取造成的竞态。
- 消息读取按 Session owner 过滤，owner 来自已验证的登录 Cookie；不存在、
  已删除或属于其他用户时统一返回 `404`。

## 认证现状

- 首版使用邮箱作为登录标识，开放注册；注册成功后直接建立登录态。
- 密码长度为 12–128 个字符，使用 Argon2id 哈希。明文密码和原始登录 token
  都不落库，数据库只保存 token 的 SHA-256 摘要。
- 登录会话默认有效期 7 天；登录时选择 `remember_me` 可延长到 30 天。
  登出会撤销当前会话；修改密码会撤销当前设备之外的其他会话。
- Cookie 名为 `athena_session`，属性为 `HttpOnly`、`SameSite=Lax`、
  `Path=/`。`ATHENA_COOKIE_SECURE=true` 时增加 `Secure`；生产环境必须启用。
- 所有非 GET/HEAD/OPTIONS 认证、项目和 Session 请求都会校验 `Origin`，
  默认按同源部署工作。`ATHENA_PUBLIC_ORIGIN` 可显式声明规范 origin。
- 登录失败返回统一 `401 invalid_credentials`；未知邮箱也执行 dummy 密码
  校验，避免通过响应或明显时序判断账号是否存在。
- 登录账号、登录 IP 和注册入口使用进程内固定窗口限流。账号窗口只按规范化
  邮箱计数，独立于客户端 IP；轮换 IP 不能绕过账号限制。该实现适合单实例
  开发和小规模部署，多实例上线前必须替换为共享限流。
- 邮箱验证、找回密码、登录设备管理和管理员禁用流程尚未实现，公网正式
  运营前需要补齐。当前也不提供跨域 CORS，前端与 API 采用同源部署。
- `users`、`auth_sessions` 表和 `projects/sessions.owner_id` 外键由迁移
  `20261105_0004` 建立。

## 本地开发

需要 Python 3.12 或更高版本。以下命令在项目根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/alembic -c backend/alembic.ini upgrade head
.venv/bin/python -m uvicorn main:create_app --factory --reload
```

运行迁移和应用前需要设置 PostgreSQL 连接：

```sh
export ATHENA_DATABASE_URL='postgresql+asyncpg://user:password@127.0.0.1:5432/athena'
export ATHENA_DATABASE_POOL_SIZE=5
export ATHENA_DATABASE_MAX_OVERFLOW=10
export ATHENA_COOKIE_SECURE=false
export ATHENA_PUBLIC_ORIGIN='http://127.0.0.1:8000'
```

可以使用 [.env.example](.env.example) 作为模板。默认地址为
`http://127.0.0.1:8000`，存活检查为 `/health`，API 文档为 `/docs`。
数据库只使用一套有界业务连接池，`ATHENA_DATABASE_POOL_SIZE` 和
`ATHENA_DATABASE_MAX_OVERFLOW` 是总容量。等待 PostgreSQL 行锁的任务会继续
占用其业务连接，因此池容量需要覆盖正常查询和并发锁等待，并受数据库连接数
上限约束。
未配置数据库且未注入任何应用服务时，应用仍可启动并只响应存活检查。
配置数据库后，应用会组装认证、项目、Session 和消息服务；从
`/api/v1/auth/register` 注册即可获得登录 Cookie。
存活检查不检查数据库或其他外部依赖。

### 开发数据库

当前开发数据库允许清空重建，不要求保留现有项目、Session 或认证数据：

```sh
.venv/bin/alembic -c backend/alembic.ini downgrade base
.venv/bin/alembic -c backend/alembic.ini upgrade head
```

也可以直接删除并重建开发数据库后执行 `upgrade head`。认证用户、项目、
Session 和消息会一起重建。

如果其他环境确实需要保留旧数据，迁移 `20261105_0003` 要求已有项目先显式
回填真实 `owner_id`，`20261105_0004` 随后要求这些 owner 对应 `users.id`。
不得使用占位所有者掩盖未知归属；含存量数据的环境必须先停写、核对用户归属
并演练迁移。

```sql
ALTER TABLE projects ADD COLUMN IF NOT EXISTS owner_id VARCHAR(128);
-- 按实际用户映射逐项目更新，禁止使用占位所有者掩盖未知归属：
UPDATE projects SET owner_id = '<实际-owner-id>' WHERE id = '<project-id>';
SELECT count(*) FROM projects WHERE owner_id IS NULL;
```

## 认证 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/auth/register` | 开放注册并建立登录 Cookie |
| `POST` | `/api/v1/auth/login` | 邮箱密码登录，可选 `remember_me` |
| `POST` | `/api/v1/auth/logout` | 撤销当前会话并清除 Cookie |
| `GET` | `/api/v1/auth/me` | 返回当前登录用户 |
| `POST` | `/api/v1/auth/password` | 修改密码并撤销其他会话 |

认证、项目和 Session 写接口拒绝与请求 origin、`ATHENA_PUBLIC_ORIGIN` 都不
匹配的 `Origin`。项目、Session 和消息接口必须携带有效 `athena_session`
Cookie；否则返回 `401 not_authenticated`。服务端不读取用户 ID 请求头，也不
提供应用级固定 principal 回退。

## 项目 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/projects` | 创建项目 |
| `GET` | `/api/v1/projects` | 分页查询有效项目 |
| `GET` | `/api/v1/projects/{project_id}` | 查询项目详情 |
| `PATCH` | `/api/v1/projects/{project_id}` | 修改项目名 |
| `DELETE` | `/api/v1/projects/{project_id}` | 软删除项目 |

列表查询支持 `page`（默认 `1`）和 `page_size`（默认 `20`，最大 `100`）。
软删除接口是幂等的：同一项目重复删除仍返回 `204`，完全不存在则返回 `404`。
仍有未删除绑定 Session 的项目不会被软删除，返回
`409 {"detail": "project_has_sessions"}`。
所有接口使用当前已验证登录用户作为所有者；其他所有者的项目统一返回
`404`，不会泄露项目是否存在。创建和改名请求体只接受 `name`，额外字段
（包括 `owner_id`）会被拒绝。
数据库不可用时返回 `503` 和 `{"detail": "database_unavailable"}`。
数据库路由同时处理 SQLAlchemy 异常和驱动建连阶段的 `OSError`；其他未来
外部 I/O 路由的 `OSError` 不会被全局吞掉。响应不暴露驱动、连接串、主机或
查询细节。

## Session API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/sessions` | 创建可绑定项目或未绑定项目的会话 |
| `GET` | `/api/v1/sessions` | 分页查询当前用户的未删除会话 |
| `GET` | `/api/v1/sessions/{session_id}` | 查询会话详情 |
| `PATCH` | `/api/v1/sessions/{session_id}` | 修改标题或首条消息前的项目绑定 |
| `DELETE` | `/api/v1/sessions/{session_id}` | 软删除会话 |

创建和修改请求体只接受 `title` 与 `project_id`，额外字段会被拒绝。创建时
两者均可省略；`title: null` 表示无标题，`project_id: null` 表示未绑定。
PATCH 使用显式字段判断更新意图，因此 `{"title": null}` 会清空标题，
`{"project_id": null}` 会解绑项目，空对象返回 `422 session_update_invalid`。
绑定项目时必须已经存在且属于当前用户；Session 产生首条消息后改绑或解绑返回
`409 session_project_locked`。

列表查询支持 `page`（默认 `1`）、`page_size`（默认 `20`，最大 `100`）、
`project_id` 和 `unbound`。软删除接口对同一 owner 幂等；Session 不存在、
已删除或属于其他用户时统一返回 `404`。当前响应只包含 `id`、`title`、
`project_id`、`created_at` 和 `updated_at`，不包含 `owner_id`、
`deleted_time` 或内部消息序号。

## 消息 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/sessions/{session_id}/messages` | 分页读取会话消息 |

查询支持 `before_sequence`（可选，正整数）和 `limit`（默认 `20`，最大
`100`）。响应页内按 `sequence` 升序，`next_before_sequence` 可用于继续
向前翻页。接口只返回 conversation messages，不返回 execution messages；
Session 不存在、已删除或不属于当前登录用户时统一返回 `404`。

Session 创建后可继续使用该接口读取 conversation messages；当前仍没有消息
发送 HTTP 接口。

## 开发检查

```sh
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy
```

仓储测试默认使用内存 SQLite。若要验证真实 PostgreSQL，使用独立测试数据库并执行：

```sh
export ATHENA_TEST_DATABASE_URL='postgresql+asyncpg://user:password@127.0.0.1:5432/athena_test'
.venv/bin/python -m pytest -m integration backend/tests/integration
```

集成测试会创建唯一的临时 schema，通过真实 Alembic 入口执行迁移，完成仓储
验证和 Session/Project 行锁并发交错测试，最后删除该 schema；不会改动
`public` schema 中的数据。

## 目录

```text
backend/src/api/               HTTP 路由、DTO 和 HTTP 转换
backend/src/api/security.py    Cookie 登录态依赖与同源写请求校验
backend/src/app/auth/          注册、登录、登出、改密和进程内限流
backend/src/domain/auth/       用户、认证会话、异常和认证端口
backend/src/app/project/       项目应用用例、结果模型和转换
backend/src/app/session/       Session CRUD 用例、结果模型和转换
backend/src/app/message/       消息写入、历史查询、结果模型和转换
backend/src/domain/project/    项目领域包：model 放实体/异常，ports 放仓储端口
backend/src/domain/message/    消息实体、类型化内容块、异常和仓储端口
backend/src/domain/session/    Session 实体、异常和仓储端口
backend/src/adapter/base.py                共享 SQLAlchemy DeclarativeBase
backend/src/adapter/models.py              聚合导入 auth/project/message/session ORM
backend/src/adapter/auth/                  用户与登录会话 model、repository、converter
backend/src/adapter/project/model/         项目 ORM 模型
backend/src/adapter/project/converter/     项目 ORM 到领域对象转换
backend/src/adapter/project/repository/    项目持久化适配器
backend/src/adapter/message/model/         会话消息与执行消息 ORM 模型
backend/src/adapter/message/converter/     parts、metadata 和 ORM 转换
backend/src/adapter/message/repository/    消息事务、查询和持久化编排
backend/src/adapter/session/model/         Session ORM 模型
backend/src/adapter/session/converter/     Session ORM 到领域对象转换
backend/src/adapter/session/repository/    Session CRUD、行锁与原子项目绑定
backend/src/common/            共享 ID 等基础工具
backend/src/infrastructure/    数据库连接、Argon2id 密码哈希等共享基础设施
backend/src/main.py            应用组装入口
backend/tests/                 后端测试
backend/migrations/            Alembic 迁移
docs/architecture.md           架构说明和待决策事项
```

架构说明见 [docs/architecture.md](docs/architecture.md)。
Session 设计见 [docs/session-design.md](docs/session-design.md)。
消息设计见 [docs/message-design.md](docs/message-design.md)。
本工程独立初始化，未复制旧 Athena 的实现。
