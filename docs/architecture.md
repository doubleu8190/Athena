# 架构起点

## 已确认需求

- 面向多用户，支持直接回答问题。
- 支持搜索、阅读、分析及生成文件。
- 支持 MCP 和 Skill。
- 支持自定义业务型 Subagent。
- 支持知识库和记忆系统。

## 当前实现

已有 Python 包、FastAPI 应用工厂、进程存活检查、项目目录 CRUD、Session
CRUD，以及消息存储和只读会话历史。项目、Session 与消息模块使用 async
SQLAlchemy 访问 PostgreSQL，使用 Alembic 管理迁移。邮箱密码认证、用户与
登录会话持久化、Argon2id 密码哈希、进程内限流和 Cookie 路由已接入应用工厂。
项目、Session 和消息接口从有效登录 Cookie 取得请求级用户身份。尚无模型
调用、Run 编排、消息发送 HTTP 接口、后台执行或前端实现。

项目主键由 `app.project.service` 中进程级共享的
`common.ids.monotonic_timestamp_id_factory()` 生成，格式为 UTC 毫秒时间字符串。
项目名去除首尾空白后必须为 1–120 个字符且不包含 Unicode 通用类别 `Cc`
的控制字符，允许重名。删除采用软删除，
所有公开查询都过滤 `deleted_time IS NULL`。

该 ID 生成器仅在单进程内保证单调，不保证跨进程、跨重启或跨实例唯一。
多进程/多实例部署前必须改为数据库序列、分布式 ID 或其他集群级唯一方案，
不能将其视为全局唯一主键。

认证、项目和 Session（含消息读取）路由将 SQLAlchemy 存储异常以及建连阶段的
`OSError` 统一映射为 `503`，响应只包含 `database_unavailable`；业务校验和
资源不存在仍分别保持 `422`、`404`。该 `OSError` 映射按数据库路由前缀限定，
未来外部 I/O 路由不会被全局吞掉。
项目存在未删除绑定 Session 时拒绝软删除，返回
`409 project_has_sessions`；项目行锁和 Session 写入时的项目共享锁使检查与
写入并发安全。

项目已落地必填的 `owner_id VARCHAR(128)`。所有者只能来自请求级
`CurrentAuth` 中已通过 Cookie 验证的用户，不接受请求体、query、客户端自报
身份、旧式用户 ID 请求头或应用级固定 principal。项目和消息路由在服务存在时
始终挂载，认证失败统一返回 `401 not_authenticated`。

项目的创建、列表、详情、改名和软删除都按 owner 限定；跨 owner 的资源统一
按不存在处理并返回 `404`，软删除幂等也只在同一 owner 内成立。HTTP 响应不
暴露 `owner_id`。

迁移 `20261105_0003` 为已有 `projects` 表增加非空 `owner_id`，把活动列表
索引替换为
`(owner_id, updated_at DESC, id DESC) WHERE deleted_time IS NULL`。迁移不会
为已有数据伪造所有者；已有行必须先由运维显式回填，再执行升级。

迁移 `20261105_0004` 创建 `users`、`auth_sessions`，并为
`projects.owner_id`、`sessions.owner_id` 添加指向 `users.id` 的外键。因此
PostgreSQL 上的项目和 Session 写入要求 owner 对应真实用户。已有项目和
Session 的回填必须使用最终会存在于 `users` 的真实用户 ID；含存量数据的
实例需要受控停写、归属核对和迁移演练，不能使用占位 owner。

迁移 `20261105_0005` 为 Session 增加 `title`、`project_id`、复合
`(project_id, owner_id)` 项目外键、活动列表索引和约束。Session 由
`app/session` 提供 CRUD，项目绑定必须属于同一 owner；首个消息写入后绑定
不可变。Session 主键使用 `sess_<uuid4hex>`，与项目使用的进程内单调毫秒 ID
不同。

认证首版以邮箱为登录标识并开放注册。密码使用 Argon2id；登录 token 使用
高熵随机值，数据库只保存 SHA-256 摘要。会话默认 7 天，`remember_me` 为
30 天，登出和改密可撤销服务端会话。Cookie 为 HttpOnly、SameSite=Lax，
生产必须通过 `ATHENA_COOKIE_SECURE=true` 启用 Secure。非安全方法的认证和
项目请求会校验 `Origin`，适用于同源部署。登录失败同时受账号和 IP 两个独立
窗口限制；账号键只依赖规范化邮箱，不随客户端地址变化。当前没有邮箱验证、
找回密码、CORS 或多实例共享限流；这些能力不能由现有首版推断为已完成。

消息模块已建立 Session `owner_id`、会话消息与执行消息两条持久化流。
当前读取接口只返回会话消息，并从已验证登录 Cookie 取得 owner，再在同一
SQL 中筛选可见 Session。工具和 Subagent 执行内容不得写入会话流，执行消息
也不提供公开读取接口。内部消息追加必须携带已认证 `owner_id`，并在写入事务
内对 owner 匹配的 Session 行执行 `FOR UPDATE` 后串行化写入；owner 不匹配与
不可见 Session 一样按不存在拒绝。assistant 回复目标、execution 来源和
tool call/result 关联必须在同一事务内通过引用校验。Session 更新、软删除和
消息追加共享同一个数据库行锁边界，不再维护额外锁对象。消息历史不调用
Session 端口做两阶段检查，而是在
同一 SQL 中 JOIN Session 表并筛选 session ID、owner 和 `deleted_time`，
使可见性判断与消息读取处于同一数据库快照。

领域代码按业务领域分包。项目领域在 `domain/project` 下用 `model/` 保存
实体、分页对象和业务异常，用 `ports.py` 声明仓储接口；领域包不依赖
FastAPI、SQLAlchemy、应用层或适配器。

适配器同样按领域分包：共享声明式基类位于 `adapter/base.py`，
`adapter/models.py` 聚合导入 auth、project、message、session 的全部 ORM
模型；认证、项目、消息和 Session 分别在
`adapter/<domain>/{model,repository,converter}` 下实现 ORM、持久化和纯转换。
仓储只承担持久化编排并调用 converter，不把 ORM 到领域对象的转换埋回查询
逻辑。应用层依赖领域 `model`/`ports`，适配器实现领域端口，HTTP 层通过应用
结果模型和 DTO 输出，不直接暴露领域或数据库内部字段。

并发控制完全依赖 Repository 在数据库事务内的行锁：Session 创建/改绑项目
使用 Project 行 `FOR SHARE`，Project 删除使用 Project 行 `FOR UPDATE` 并
检查未删除绑定 Session；Session 更新、软删除和三类消息追加使用 Session 行
`FOR UPDATE`。这些约束跨连接、跨进程生效，并在事务提交或回滚时自动释放，
不需要额外锁层或连接清理协议。

数据库资源由 `Database` 统一管理，只保留一套有界业务 `AsyncEngine` 和连接池。
容量由 `ATHENA_DATABASE_POOL_SIZE`、`ATHENA_DATABASE_MAX_OVERFLOW` 控制，
`dispose()` 释放该池。行锁等待会占用一个业务连接，因此容量要覆盖正常查询与
并发锁等待，并受 PostgreSQL 连接数上限约束。

## 初始技术选择

本次初始化采用 Python + FastAPI 后端和模块化单体结构。
这是本次工程初始化的技术选择，后续可在明确需求后调整。
模块化单体指在一个代码库内按业务职责分模块，不要求拆分微服务。
执行器将来是否独立进程部署，由任务时长、隔离与恢复需求决定。

采用 DDD 的业务语言、职责边界和业务不变量思想；暂不统一套用聚合根、
领域事件、CQRS、事件溯源或通用仓储框架。具体模块出现复杂规则时，
再引入能够解决实际问题的模式。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| 项目 | 项目目录、名称维护、查询和软删除 |
| 用户与授权 | 身份、资源归属、权限和用户工具凭据 |
| 会话 | Session 目录、消息及用户交互 |
| 执行 | Agent 循环、任务、子任务、取消和恢复 |
| 能力 | 内置工具、MCP、Skill 的发现和调用 |
| Subagent 定义 | 业务指令、能力配置、版本与可见范围 |
| 文件 | 上传、中间文件、最终产物及访问权限 |
| 知识库 | 资料解析、索引、检索与引用 |
| 记忆 | 跨会话信息的提取、读取、修订与删除 |

按实际开发进度创建模块，避免预先生成大量空目录和接口。
项目、Session、消息和认证已完成首个切片并接入 `create_app`。其余模块尚未实现。
简单模块可以直接采用服务与存储适配器；复杂规则可在模块内部增加领域层。
HTTP 入口负责协议转换，业务服务负责用例，外部 SDK 和存储实现在适配器边界。
模块之间通过明确的公共接口协作，不直接修改其他模块的持久化数据。
不创建包揽所有业务的公共模型目录或基础服务类。

## 设计约束建议（需要在后续实现中落实）

- 项目 `owner_id` 只能由服务端验证后的 `CurrentAuth` 注入，不能依赖客户端
  自报用户身份；新增资源必须继续保留这一约束。
- `users` 和 `auth_sessions` 已由 0004 建立，
  `projects/sessions.owner_id` 已指向 `users.id`；仍需明确删除用户时项目、
  Session 和消息的保留或级联策略。
- `ATHENA_COOKIE_SECURE` 控制登录 Cookie 的 Secure 属性，生产必须开启；
  `ATHENA_PUBLIC_ORIGIN` 用于同源写请求校验。公网部署前需要补齐邮箱验证、
  找回密码和共享限流；Session CRUD 已按 owner 完成资源权限检查。
- 已有项目和 Session 数据升级到 `0003 -> 0005` 前必须由运维显式回填真实
  owner；不得在迁移或应用启动过程中猜测、伪造或静默忽略未知归属。
- Session 更新、软删除和消息追加必须锁定同一 Session 行；Session 创建/改绑
  必须锁定目标 Project 行并重新检查可见性；Project 删除必须锁定 Project 行
  后检查未删除绑定 Session。不得在应用服务中增加绕过仓储行锁的写路径。
- `Database` 只保留一套有界业务 engine。行锁等待占用业务连接，池容量通过
  `ATHENA_DATABASE_POOL_SIZE/MAX_OVERFLOW` 配置，并同时覆盖正常查询、锁等待
  和 PostgreSQL 总连接数限制。
- 当前并发方案只保护数据库已有行。没有数据库行的资源不能依赖该机制；若未来
  出现真实需求，必须重新设计并确认，不预先引入通用锁抽象。
- 身份与资源权限由服务端检查；不能依赖模型提供的用户标识。
- 主 Agent 和 Subagent 复用执行机制；Subagent 的权限不能超出用户授权。
- MCP 提供能力，Skill 提供操作指引；加载 Skill 不会获得额外权限。
- 会话记录、任务执行记录、知识库和长期记忆分别管理。
- 任务恢复依赖持久化执行状态；有外部副作用的工具需明确重试和幂等语义。
- 用户文件和代码执行需要隔离环境，产物下载也需要检查权限。

## 待决策

- 首个端到端用例、模型提供方和模型调用协议。
- 邮箱验证、找回密码、登录设备管理，以及项目是否需要组织级共享或额外的
  成员权限模型。
- 文件存储和后台任务机制。
- 前端技术栈、事件推送与任务交互协议。
- MCP 接入方式、Skill 格式及可信来源。
- 知识检索方案和自动记忆的写入策略。

先实现身份隔离下的单轮问答闭环，再逐步接入工具、文件与其他能力；
具体实施顺序需要在选定首个用例后确认。
