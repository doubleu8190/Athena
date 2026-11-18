# Athena v2

面向多用户的通用智能助手。

已确认的产品范围：直接回答、搜索阅读与分析、文件生成、MCP、Skill、
自定义业务 Subagent、知识库和记忆系统。

当前已实现邮箱密码认证、项目与 Session 目录、消息存储和只读历史、全局
MCP Server 管理、安全工具目录 API，以及首期 Run 后端闭环。认证使用服务端
可撤销登录会话和 HttpOnly Cookie；管理操作在应用服务及最终写事务中复核当前
角色和会话。Run 支持提交、澄清恢复、取消、失败重试、独立 Worker 执行和 SSE
临时增量；已接入管理员 MCP 工具与只读的 `memory_search`、`knowledge_search`，
生产前端阶段 1–5 已完成实现与独立评审验收：真实认证与项目/Session 目录、
对话侧栏项目树、新对话草稿、项目中心四 Tab、会话消息、Run/SSE、停止、
补充输入、工具审批、失败重试和执行轨迹，以及知识库目录/文档/版本/来源/检索、
项目知识库关联和模型/MCP/工具管理控制台均已接入。

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
- Session CRUD 与 Run HTTP 接口分开；正式消息写入由内部 `MessageService` 提供，
  由 Run 命令在共享数据库事务内调用。

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
- 消息写入只由内部应用服务提供，并由 Run 命令在共享事务中调用；不提供独立的
  通用消息写入 HTTP 接口。
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

需要 Python 3.12 或更高版本；运行完整容器栈还需要 Docker Desktop 或
Docker Engine。以下命令在项目根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm --prefix frontend install
cp .env.example .env
```

`.env` 只用于在宿主机直接运行 Alembic、API 和 Worker；Docker 测试栈使用
`docker/test.env` 和 `secrets/`，两类配置不要互相复制。编辑 `.env`，至少设置可访问的
`ATHENA_DATABASE_URL` 和随机生成的 `ATHENA_MCP_SECRET_KEY`。本地开发可用以下
命令生成密钥，再将结果粘贴到 `.env`：

```sh
python3 -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())'
chmod 600 .env
```

也可以在宿主机单独启动前端或后端服务（如本地调试），后端命令如下：

```sh
set -a
source .env
set +a
.venv/bin/alembic -c backend/alembic.ini upgrade head
.venv/bin/python -m uvicorn main:create_app --factory
```

`alembic` 和直接运行的 Python 进程都不会自动读取 `.env`；修改该文件后需要在
当前终端重新执行上面的 `source`。也可以不使用文件，在当前终端显式设置：

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

### 本地 Docker 测试依赖

项目根目录的 [compose.test.yml](compose.test.yml) 提供一套独立的 PostgreSQL、Redis
和 MinIO 测试依赖。PostgreSQL 同时创建 `athena` 开发库和 `athena_test` 测试库，
并预装迁移需要的 `vector` 与 `zhparser` 扩展；MinIO 初始化知识库和会话文件各自
独立的开发/测试私有桶。Compose 项目名和持久数据目录与其他项目隔离，端口只绑定本机：
PostgreSQL `5432`、Redis `6379`、MinIO S3 `9000`、MinIO Console `9001`。
PostgreSQL 基于固定摘要的 PostgreSQL 18.6/pgvector 0.8.7 镜像，并从固定上游
`zhparser` commit 和带 SHA-256 校验的 SCWS 1.2.3 源码构建扩展；Redis 固定为 7.4.7，
MinIO 固定为 `RELEASE.2025-10-15T17-29-55Z` 镜像摘要。

首次使用时，如果本地配置文件还不存在，复制非敏感设置模板；再运行 secret 初始化工具。
已有环境默认必须提供完整旧凭据，工具核对 PostgreSQL URL、MinIO app/test 与 root 凭据，
全部一致后才迁入 `secrets/` 并从本地配置删除敏感键。若缺少旧凭据或来源冲突，工具会
停止且不写入或清理文件。仅全新环境可显式使用 `--fresh` 生成随机凭据；不要对已有数据
目录或遗留命名卷使用该选项。已有 secret 不会被覆盖。

两份本地配置的职责不同：

- `.env`：宿主机直接运行 API、Worker 和 Alembic 时使用。
- `docker/test.env`：Docker 测试栈唯一使用的非敏感配置，包含端口、数据库名、桶名及
  应用测试参数。

真实随机凭据只保存在 `secrets/`。`scripts/test-docker.sh` 从 `docker/test.env` 读取
非敏感值，从 `secrets/` 派生容器连接配置；Compose 使用清理后的白名单环境并显式禁用
根目录 `.env`，因此当前 shell、`.env` 或旧 `.env.test` 中的同名变量不会覆盖测试配置。

```sh
if [ ! -f docker/test.env ]; then cp docker/test.env.example docker/test.env; fi
scripts/bootstrap-test-secrets.py --fresh  # 仅全新且没有旧数据时
# 有旧配置/凭据的已有环境运行：scripts/bootstrap-test-secrets.py
```

bootstrap 成功后无需再生成第二个配置文件。已有环境先运行不带 `--fresh` 的 bootstrap；
bootstrap 会删除旧版固定 MinIO endpoint，helper 会按当前数据库名、`MINIO_API_PORT`
与桶名重新生成连接地址。

PostgreSQL 和 MinIO 启动时从 Compose secrets 读取用户及密码。MinIO 镜像自身不支持
`_FILE` 变量，由精简入口包装器从只读 secret 读取 root 凭据；桶初始化容器也只读取
同一 secret。宿主 API 配置通过 shell helper 在当前终端生成：

```sh
source scripts/load-test-env.sh
```

helper 从 `secrets/` 读取 PostgreSQL 与 MinIO 凭据，URL 编码 PostgreSQL 连接地址，
并设置 `ATHENA_TEST_DATABASE_URL`、`ATHENA_REDIS_URL`、MinIO endpoint、桶名及 MCP 加密密钥；
endpoint 和开发/测试桶名分别从 `MINIO_API_PORT`、`MINIO_DEV_BUCKET` 与
`MINIO_TEST_BUCKET` 派生。旧配置中的应用桶名若与 Compose 桶名不一致，bootstrap 会停止
并保留配置，先统一本地桶名再重试。
本地知识库和测试访问使用同一个 MinIO root 用户，因此共用其实际 root secret。
Redis 保持无密码配置。凭据不会写入模板、README 或根目录 `.env.example`。
Compose 数据现在直接绑定到忽略跟踪的 `data/postgres`、`data/redis` 和 `data/minio`；
原有三个命名卷的数据已复制过去并保留为回滚副本。

`scripts/test-docker.sh` 会等待 PostgreSQL、Redis 与 MinIO 健康，再运行一次性
`minio-init`；成功退出表示四个租户隔离桶已创建。
迁移前先导入本地 helper，再运行 Alembic。该连接明确指向新建的
开发数据库 `127.0.0.1:5432/athena`，不会迁移项目中其他数据库：

```sh
source scripts/load-test-env.sh && \
  .venv/bin/alembic -c backend/alembic.ini upgrade head
```

运行唯一容器服务入口：

```sh
scripts/test-docker.sh
```

该脚本自动拉起 PostgreSQL、Redis 和 MinIO，按需预构建
`athena-backend-base:latest`，从 `secrets/` 同步容器凭据，执行
`POSTGRES_TEST_DB` 与测试桶对应的数据库迁移，并启动 frontend、API、Run Worker、
Memory Worker 等业务容器。脚本启动后通过 `docker compose port frontend 80` 读取实际
宿主机端口再探活 `/health`；`docker/test.env` 默认端口为 `8080`。常用命令：

- `scripts/test-docker.sh up --build-base`：强制重新构建后端基础依赖镜像并启动。
- `scripts/test-docker.sh status`：查看业务容器与基础设施容器状态。
- `scripts/test-docker.sh logs -f`：实时跟踪业务容器日志。
- `scripts/test-docker.sh down --all`：停止业务容器与底层基础设施。

`ATHENA_KNOWLEDGE_ENABLED` 按后端规则忽略大小写并去除首尾空白，接受
`true/false`、`1/0`、`yes/no` 和 `on/off`。

运行宿主机后端集成测试（pytest）时，先在终端加载测试环境变量（`source scripts/load-test-env.sh`），其中 `ATHENA_TEST_DATABASE_URL` 指向独立的
`athena_test` 数据库，`ATHENA_REDIS_URL` 指向本 Compose 的 Redis。Compose 的
`--env-file` 只提供端口、数据库名和桶名等非敏感配置，不会自动导出给宿主机上的
Alembic 或 pytest。
`docker/test.env` 中默认启用知识库配置；如需关闭容器栈的知识库，在该文件中显式设为
`ATHENA_KNOWLEDGE_ENABLED=false`。测试后执行 `scripts/test-docker.sh down --all`
停止全部容器；`data/postgres`、`data/redis` 和 `data/minio` 中的 bind mount 数据会保留。

## 前端开发

生产前端位于 `frontend/`，阶段 1–5 已完成实现与独立评审验收。它已接入真实
项目与 Session 目录、项目树展开、新对话项目管理、
项目中心四 Tab、Session 分页/改名/删除、`/chat/:sessionId` 的消息历史、
Run 提交、SSE 运行态、停止、补充输入、工具审批、失败重试和执行轨迹，以及
知识库、项目知识库关联和管理控制台。新草稿只在首次发送时创建 Session；
知识库和管理页面不再显示占位空态，也不展示原型中的示例数据。

```sh
cd frontend
npm install
npm run dev
```

Vite 默认运行在 `http://localhost:5173`，并将 `/api` 代理到
`http://127.0.0.1:8000`。开发时请通过 Vite 地址访问，以保持 Cookie 与
写请求同源校验；生产同源托管方案尚未确定。

类型检查、测试和构建：

```sh
cd frontend
npm run typecheck
npm run test
npm run build
```

认证、路由、数据缓存和阶段边界见 [`frontend/README.md`](frontend/README.md)。

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

Session 创建后可继续使用该接口读取 conversation messages；用户消息通过
`POST /api/v1/sessions/{session_id}/runs` 随 Run 提交写入，当前仍没有独立的
通用消息写入 HTTP 接口。

## MCP 管理与工具目录 API

管理员接口要求当前数据库角色为 `admin`；普通用户访问管理路径统一返回
`403 admin_required`，未登录返回 `401 not_authenticated`。首次管理员由部署方
为已有 active 账号执行受控本地命令：

```sh
cd backend
../.venv/bin/python -m cli.promote_admin --email admin@example.com
```

该命令不会创建账号或设置默认密码。注册接口不能提交 role。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/admin/mcp/servers` | 发现并注册 HTTP 或 stdio MCP Server |
| `GET` | `/api/v1/admin/mcp/servers` | 分页查看全局 Server |
| `GET` | `/api/v1/admin/mcp/servers/{server_id}` | 查看连接配置和秘密名称/是否已设置 |
| `PUT` | `/api/v1/admin/mcp/servers/{server_id}` | 带 `expected_config_version` 完整替换配置 |
| `DELETE` | `/api/v1/admin/mcp/servers/{server_id}` | 删除 Server 与工具目录 |
| `GET` | `/api/v1/admin/mcp/servers/{server_id}/tools` | 查看指定 Server 的工具来源 |
| `GET` | `/api/v1/admin/tools` | 分页查看全局工具及来源 |
| `GET` | `/api/v1/admin/tools/{tool_id}` | 查看单个工具来源 |
| `PATCH` | `/api/v1/admin/tools/{tool_id}` | 修改已注册 MCP 工具的运行期审批策略 |
| `GET` | `/api/v1/tools` | 任一有效用户读取安全工具目录 |
| `GET` | `/api/v1/tools/{tool_id}` | 任一有效用户读取单个安全工具 |

列表支持 `page`（默认 `1`）和 `page_size`（默认 `20`，最大 `100`）。写请求
拒绝混合 transport 字段及额外字段；Server 修改是完整替换。公共工具响应只含
`tool_id`、`model_name`、`description` 和 `input_schema`。管理响应也不会返回
秘密值、密文、nonce 或密钥，并额外返回 `requires_approval`。策略接口请求体只
允许 `{"requires_approval": true|false}`，默认关闭；重新发现工具时按
`remote_name` 保留已有策略，运行中的 Run 继续使用创建时的工具快照。
公共工具列表只接受 `page`、`page_size` 且不接受重复参数；工具详情不接受 query
参数。已认证请求携带其他或重复 query 时返回固定 `422 mcp_query_invalid`。

数据库默认组装 MCP 服务时必须配置 `ATHENA_MCP_SECRET_KEY`（base64 编码的
32-byte 随机密钥）；`ATHENA_MCP_SECRET_KEY_VERSION` 默认为 `1`。不得将密钥提交
到仓库或日志。`ATHENA_MCP_ALLOWED_PRIVATE_HOSTS` 是逗号分隔的精确主机白名单，
默认空集并拒绝私网连接。`ATHENA_MCP_STDIO_BASE_ENV_NAMES` 列出可从宿主环境
复制到 stdio 子进程的变量名，默认空集；每个 Server 的环境秘密单独配置，进程
不会继承完整宿主环境。API 与 Run Worker（含工具执行）使用同一套严格解析：
空项、非法主机或非法环境变量名都会导致进程启动失败。HTTP 只允许 HTTPS、固定
DNS 解析地址并阻断重定向；发现受总时限、响应大小、页数和工具数限制。

模型业务调用不通过公开的通用 `tools/call` 路由，而是由 Run 按冻结目录执行；
每次调用独立建立并关闭 HTTP/stdio MCP 连接，SDK 自动重试关闭。MCP 健康探测和
定时刷新尚未实现；普通工具目录查询只读取最近一次成功发现并持久化的结果。
Run、LLM 配置及 Redis/SSE 的实现边界见下文。

### 随后端镜像部署 stdio Server

管理员维护的 Python stdio MCP Server 放在仓库根目录 `mcp_servers/`，后端镜像会
原样复制到 `/app/mcp_servers/`。仓库提供 `mcp_servers/payroll/` 作为测试示例，
部署时不应把它当作生产业务 Server；运行时不安装额外依赖，只允许使用
`athena-backend:latest` 中已有的 Python 依赖。具体目录约定和限制见
[`mcp_servers/README.md`](mcp_servers/README.md)。

注册时使用镜像内的绝对解释器和绝对脚本路径。例如脚本位于
`mcp_servers/example/server.py`：

```json
{
  "name": "example",
  "transport": "stdio",
  "command": "/usr/local/bin/python",
  "args": ["/app/mcp_servers/example/server.py"],
  "cwd": "/app/mcp_servers/example",
  "secrets": {}
}
```

API、Run Worker 及其他后端服务共用 `athena-backend:latest`。修改 `mcp_servers/`
后必须重建镜像并重新创建相关容器，不能依赖运行中容器热加载：

```sh
docker compose build backend-api backend-worker
docker compose up -d --force-recreate backend-api backend-worker
```

若部署了 `backend-memory-worker` 或 `backend-migration`，也应使用新镜像重建并
重新创建。注册前先确认容器内脚本路径存在，再通过管理 API 完成 MCP 发现。

## LLM 调用模块

生产 Run 路径使用 Chat Completions；管理员在后台配置模型版本和 API Key，密钥以
部署主密钥加密保存，接口只返回配置状态。旧环境变量中的 Profile 和供应商密钥不再
参与生产配置。升级时旧密钥不会迁移：相关模型版本会停用，默认模型与当前压缩策略
指针会清除。管理员需重新录入 API Key、选择默认模型并重新保存压缩策略。历史
Run 仍保留原模型和压缩策略快照；若后续仍需调用没有密钥的历史模型，该 Run 会失败。
压缩策略指针仍会供新 Run 固定；若策略模型停用或密钥不可用，Run 会回退到完整
上下文继续执行，超出模型上下文窗口时才会失败。请重新启用有效模型并保存新策略版本。

原低层 Responses 服务、`app.state.llm_service` 注入位及 `LlmService`/`LlmProfile`
已删除；应用不会从环境变量自动构建模型服务。共享 HTTP 响应限流工具位于
`backend/src/adapter/llm/bounded_http.py`，仅由 Chat Completions 适配器使用。
管理后台配置驱动 Run Worker 的 Chat Completions 适配器。模型版本的
`supports_parallel_tool_calls` 默认为 `false`；开启时必须同时支持工具调用。

## 记忆核心

长期记忆沿用 `ATHENA_MEMORY_ENABLED` 总开关，默认关闭。启用后，API 和 Run
Worker 加载本地 `BAAI/bge-m3`、`BAAI/bge-reranker-v2-m3` 与 BGE tokenizer；
Memory Worker 加载文本模型适配、embedding 和预算计数所需 tokenizer，不加载 reranker。模型必须预先放在
本地缓存中，启动不会下载权重。API、Run Worker 与 Memory Worker 必须使用同一数据库、
embedding 模型和固定 revision；不一致的 `embedding_profile_id` 会阻止旧向量被误用。
模型 revision 由 `.env.example` 固定，部署方只应通过管理员模型配置切换文本模型。

`ATHENA_MEMORY_LIFECYCLE_ENABLED=true` 还需与总开关同时启用。它负责终态 Run 的
多 Run 联合提取、向量/全文 Top-K 检索与一次批量 ADD/UPDATE/IGNORE 决策、可靠唤醒扫描、
每日归档和项目删除清理。正常有候选时调用文本模型两次：提取一次、整批决策一次，不逐条追加语义评审。首次启用
从 Memory Worker 启动时间建立扫描边界，只处理此后新完成的 Run，不执行全历史回扫。
Run 固定 owner、Session、项目、来源消息和模型版本。单个 Memory Worker 顺序处理
完整 Run 批次，模型调用不持数据库锁；提交时锁用户状态行，复核 generation、成员、来源和设置，
一次事务写入记忆与所有成员 Run 的处理状态。失效结果丢弃，超长单 Run 跳过，不分片。
不支持同时运行多个 Memory Worker。普通 running 任务在 Worker 启动时恢复，已提交批次按
Run 状态去重，不使用记忆任务租约、心跳或逐片恢复协议。

读取只把 `always` 记忆作为优先固定上下文，`on_demand` 仅由检索工具获取；预算按完整
条目计算，超预算条目不进入请求，避免正文被截断。每个模型请求前都会重新复核固定
记忆和历史 `memory_search` 结果的 ID、版本与 scope；删除、纠正或关闭读取后，旧工具
正文会从当前请求投影中移除，同时保留 tool call/result 配对。可通过
`ATHENA_MEMORY_CONTEXT_PERSONAL_TOKENS`、`ATHENA_MEMORY_CONTEXT_PROJECT_TOKENS`
和 `ATHENA_MEMORY_CONTEXT_PER_SCOPE_LIMIT` 调整上下文边界。

同步 `remember`、`correct`、`forget` 与明确整项目清理沿用 Run 工具审批链；只有事务
提交成功才反馈完成，响应丢失时按控制键读取同事务回执。删除使用 `deleted_time` 无正文
墓碑，不新增 `deleted` 生命周期状态；正文、来源、分类、加载策略和向量都会
清空。数据库约束允许墓碑字段为 `NULL`，降级时为满足旧 schema 使用不含原内容的兼容
哨兵。历史聊天正文不会被撤回。

API 不因管理员没有配置默认文本模型而启动失败，读取和同步控制仍按各自配置工作；
Memory Worker 启动时会明确验证默认模型版本，缺失时失败而不是静默跳过自动提取。
Worker 的轮询、批次、输出和归档参数见 `.env.example` 的
`ATHENA_MEMORY_*` 配置。检索阈值仍可通过
`ATHENA_MEMORY_VECTOR_MIN_SCORE`（默认 `0.70`）、
`ATHENA_MEMORY_RERANK_MIN_SCORE`（默认 `0.30`）和
`ATHENA_MEMORY_TIMEOUT_SECONDS`（默认 `60`）调整；只有明确校准后才配置
`ATHENA_MEMORY_KEYWORD_FALLBACK_MIN_SCORE`，重排失败时不默认接纳关键词候选。
重排阈值 `0.30` 来自少量中文正负样例的初始测量，部署前应在目标记忆语料上复核。

单独启动 Memory Worker：

```sh
.venv/bin/python -m memory_worker
```

Docker Compose（通过 `scripts/test-docker.sh` 启动）中的
`backend-memory-worker` 会在两个记忆开关同时启用时持续运行；关闭时以状态码 `0` 退出，
真实异常则由 `on-failure:5` 有界重启。迁移需要 PostgreSQL `vector` 和 `zhparser`
扩展。记忆生命周期收缩及检索决策采用增量迁移 `20261125_0026`、`20261126_0027`，验证范围见
[记忆验收报告](docs/memory-lifecycle-e2e-report.md)。

## 知识库与 MinIO

知识库原件只写入部署级私有 MinIO 桶，PostgreSQL 继续保存目录、版本、解析文本、
分块、索引和任务状态；手动文本仍保存在数据库。本地只用于上传暂存、解析临时文件、
OCR 与模型缓存。启用知识库时安装额外依赖：

```sh
.venv/bin/python -m pip install -e '.[dev,knowledge]'
```

Python 3.14 当前没有兼容的 PaddleOCR/PaddlePaddle 组合，可在该环境运行非 OCR
单元测试，但不要在 Python 3.14 启动知识库 worker。MinIO SDK 固定在
`minio==7.2.20`；关闭知识库时不读取 MinIO 配置，也不导入 SDK 客户端。

桶必须由部署方预建并保持私有，应用不会创建桶或修改策略。服务凭据需要桶检查、
对象列举、读取、上传和删除权限。API、`knowledge_worker` 与启用知识查询的
`run_worker` 必须使用相同数据库、模型 revision 和目标桶：

```text
ATHENA_KNOWLEDGE_ENABLED=true
ATHENA_KNOWLEDGE_MINIO_ENDPOINT=host[:port]
ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY=<由 secret manager 注入>
ATHENA_KNOWLEDGE_MINIO_SECRET_KEY=<由 secret manager 注入>
ATHENA_KNOWLEDGE_MINIO_BUCKET=<预建私有桶>
ATHENA_KNOWLEDGE_MINIO_SECURE=true
```

TLS 默认开启，布尔值必须明确；证书校验使用系统信任库，部署私有 CA 时可通过
`SSL_CERT_FILE` 指定 CA 文件，关闭 TLS 只限受控 HTTP 环境。适配器默认连接超时
5 秒、读取超时 30 秒、最多 2 次重试，只重试幂等方法；每进程最多 4 个并发同步
存储操作。对象键、字节大小和 SHA-256 会在写入前和读取后校验，已有对象会读取
实际内容复核，下载响应始终关闭并释放连接。应用不管理未完成分段上传，部署方应在
专用桶配置其生命周期回收；完成对象超过 24 小时且无来源或预留引用时才由 worker
清理。MinIO 暂不可用不影响文档的逻辑删除，后台任务会按策略退避重试。

真实 MinIO 测试使用独立的 `ATHENA_TEST_MINIO_*` 桶配置；未提供环境时测试明确
跳过。写入过新对象后不能退回旧本地原件目录，故障时应暂停写入并修复 MinIO 配置。
更完整的启动、权限、切换与回滚边界见
[知识库运行环境](docs/knowledge-base-operations.md)。

## Skills 与会话文件

Skills 和会话文件使用独立的 `ATHENA_FILE_MINIO_*` 私有桶，不依赖知识库开关，
也不会加载 embedding、OCR 或 Paddle 依赖。API 和 Run Worker 必须指向同一数据库
和文件桶；脚本解释器应与 Worker 使用同一 Python 环境。启用前预建私有桶，并给
服务账号桶检查、读取、上传、删除和列举权限：

```text
ATHENA_FILES_ENABLED=true
ATHENA_FILE_MINIO_ENDPOINT=host[:port]
ATHENA_FILE_MINIO_ACCESS_KEY=<由 secret manager 注入>
ATHENA_FILE_MINIO_SECRET_KEY=<由 secret manager 注入>
ATHENA_FILE_MINIO_BUCKET=<预建私有桶>
ATHENA_FILE_MINIO_SECURE=true
ATHENA_SKILL_TEMP_ROOT=/srv/athena/skill-executions
ATHENA_SKILL_PYTHON_EXECUTABLE=/srv/athena/.venv/bin/python
```

API 和 Worker 使用同一个 `ATHENA_SKILL_PYTHON_EXECUTABLE` 做真实依赖探测并运行
脚本；未设置时两类进程都退回各自的 `sys.executable`，跨环境部署应显式设置绝对
路径。用户上传单文件上限 50 MiB，单会话上传与脚本产物合计最多 100 个文件；
生成产物没有单文件 50 MiB 限制。Skill ZIP 上限 25 MiB、解压后 100 MiB、最多
500 个文件。执行日志保留上限为 5 MiB。API 和 Worker 不自动回收对象，也不扫描
遗留临时目录。对象回收只在停写维护窗口使用离线命令：默认 dry-run，`--execute`
必须同时确认所有写入者已停止；命令从桶起点分页扫描，并复核会话文件、Skill 版本和
执行日志三类数据库引用。不要给对象前缀配置按年龄删除的生命周期规则。升级顺序、
MinIO 数据保留、离线维护和真实闭环验证见
[Skills 与会话文件运行手册](docs/skills-files-operations.md)。

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

## Run 执行

Run 是 Session 中一次任务的持久化执行。迁移至当前 Alembic head 后，API 负责
提交任务、澄清回答、取消、重试及 SSE；独立 Worker 领取 PostgreSQL 队列并调用
Chat Completions。新 Run 与 retry 固定使用 `run-v2`，
每轮可接受 1–8 个业务工具调用并形成同一批次；同一 Run 最多 4 个调用实际并发，
结果按调用序号稳定交回模型。批次中任一调用需要管理员配置的运行期审批时，整批
暂停且免审批调用也不会提前执行；用户逐个批准或拒绝，最后一个决定原子地把 Run
重新排队，Worker 从持久化批次恢复而不重放模型请求。明确失败不扩散，unknown
停止派发新调用、等待已开始调用收敛后失败 Run。Sub Agent 尚未接入。增量文本只
用于实时展示，断线后重新读取数据库快照和正式消息，不回放历史 token。

每 Run 最多 40 次实际 LLM 请求，正常生成、压缩和无输出临时错误的应用层重试
共用额度；计数在 provider 调用前原子增加，失败不退还。工具本身不计次，单次
默认 30 秒并受执行段剩余时间限制。参数非法、未知工具或暂不可用仍保存调用/结果
对；参数超过 64 KiB、非法 JSON 或缺失调用 ID 按协议失败处理。工具结果在 UTF-8
字节和 JSON 结构边界内受限，超长时明确标记截断。

### 模型配置

只有管理员可以创建配置版本和设置默认版本。管理员通过后台提交 API Key，服务端
以 AES-GCM 密文保存在数据库中，API 只返回是否已配置。API 与 Worker 部署需要
提供用于密钥封装的 `ATHENA_MCP_SECRET_KEY` 主密钥；它不是模型配置或供应商凭据。

管理员接口为 `/api/v1/admin/llm/model-profiles`。创建版本示例：

```json
{
  "name": "primary",
  "base_url": "https://api.example.com/v1",
  "model": "provider-model-name",
  "api_key": "由管理员安全提交的模型密钥",
  "context_window_tokens": 128000,
  "tokenizer_encoding": "o200k_base",
  "max_output_tokens": 4096,
  "request_timeout_seconds": 60,
  "execution_timeout_seconds": 300,
  "max_concurrency": 4,
  "temperature": null,
  "supports_parallel_tool_calls": false
}
```

仅支持 `cl100k_base` 和 `o200k_base` 计数。Worker 启动时会在独立有界线程预热
两种编码，最多等待 45 秒；部署时应预置可写 tokenizer cache，或允许首次启动下载
资源。预热失败或超时会拒绝启动 Worker。运行中上下文转换和 token 计数使用最多
4 个 daemon 工作线程，容量满时异步排队且等待计入 Run deadline。超时不会强杀正在
运行的线程；其工作槽位在实际退出后释放，超时 Run 不会继续调用模型。当前只支持
`run-v2`；历史 `run-v1` Run 执行时会以 `run_prompt_template_unsupported` 安全
失败；新提交和 retry 固定使用 `run-v2`；
上下文预算会计入系统提示、消息协议、工具调用 schema 和预留输出 token，超限
时明确失败，不静默截断。创建后通过 `PUT /api/v1/admin/llm/model-profiles/default`
设置 `{ "profile_id": "<返回的版本 ID>" }`。禁用版本会阻止后续调用；密钥轮换后
重启 API 与 Worker 进程，使环境变量更新生效。被引用的旧版本保留供历史 Run
审计，不能删除或改写。

### Worker 与实时传输

API 和 Worker 使用同一 `ATHENA_DATABASE_URL`。单独启动 Worker：

```sh
.venv/bin/python -m run_worker
```

`ATHENA_REDIS_URL` 可选。Redis 暂时不可用时 Run 仍会持久化和完成，SSE 通过
数据库周期对账交付正式消息，并可能发送 `stream_interrupted`。反向代理必须关闭
SSE 响应缓冲，并设置高于连接预期时长的读取超时；应用默认每 15 秒发送心跳。
Worker 参数可通过 `.env.example` 中的 `ATHENA_RUN_*` 项调整：默认并发 4、租约
30 秒、每 5 秒续租、执行总时限 300 秒、取消/队列轮询 1 秒、租约回收 5 秒；
LLM 请求上限默认 40，工具超时 30 秒，参数与规范化结果上限均为 65536 字节，
资源清理最多额外 10 秒；单批工具上限 8、实际并发默认 4（1–8，且不超过批次
上限）。数据库截止时间依赖各服务主机时钟同步。

工具调用可通过
`GET /api/v1/sessions/{session_id}/runs/{run_id}/tool-calls` 和
`GET .../tool-calls/{call_id}` 查询。列表按 `after_ordinal` 与 `limit` 分页，
摘要不含参数和结果；详情只返回有界的参数、规范化 outcome、状态、耗时和截断标记，
不返回 MCP server/remote 名称、URL、凭据或 SDK 异常。所有查询复核 owner 和会话
可见性，不存在与不可见统一返回 `404`。

等待审批的 Run 详情包含安全的 `pending_approvals` 投影（调用 ID、批次消息 ID、
序号、模型工具名、类型和请求时间），完整参数仍通过调用详情读取。逐调用审批：

```text
POST /api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/{call_id}/approval
{"request_id": "client-idempotency-key", "decision": "approve"}
```

`decision` 只允许 `approve` 或 `reject`；同键同内容幂等，同键不同内容或相反决定
返回 `409`。审批不使用 Conversation 消息或 `run_input_requests`，等待期不自动
过期，但用户可以取消 Run。

`POST .../retry` 始终创建从头执行的新 Run，可能重复此前成功的写操作；响应中的
`retry_warning` 会明确说明。来源 Run 存在 unknown 工具结果时，请求必须带
`{"request_id": "...", "confirm_unknown": true}`；未确认返回
`409 tool_retry_confirmation_required`。确认只针对整个 unknown 事实，不支持部分
调用确认或结果复用；旧 unknown 记录保持不变。

升级数据库使用 `.venv/bin/alembic -c backend/alembic.ini upgrade head`。含旧数据的
部署应先备份并按迁移说明核对 owner 归属；缺少有效所有者的历史数据会被拒绝迁移，
不能用占位 owner 绕过。Run/LLM 迁移的 downgrade 会删除模型版本、Run、事件、请求
及相关 managed 关联列，属于破坏性操作。迁移脚本在部分历史数据冲突时会拒绝降级；
即使执行通过也会丢失这些数据。生产环境不要直接 downgrade；需要回滚时从已验证的
备份恢复，并先在隔离环境演练。

联合集成测试使用真实 PostgreSQL、Redis、公开 Cookie 认证与 Worker，但模型提供方
为确定性 fake streaming provider。真实供应商联调需要在部署环境配置有效密钥后另行
执行；当前测试结果不代表供应商兼容性验证。

## 目录

```text
backend/src/api/               HTTP 路由、DTO 和 HTTP 转换
backend/src/api/security.py    Cookie 登录态依赖与同源写请求校验
backend/src/app/auth/          注册、登录、登出、改密和进程内限流
backend/src/domain/auth/       用户、认证会话、异常和认证端口
backend/src/app/project/       项目应用用例、结果模型和转换
backend/src/app/session/       Session CRUD 用例、结果模型和转换
backend/src/app/message/       消息写入、历史查询、结果模型和转换
backend/src/app/tool/          MCP 管理与工具目录用例
backend/src/domain/project/    项目领域包：model 放实体/异常，ports 放仓储端口
backend/src/domain/message/    消息实体、类型化内容块、异常和仓储端口
backend/src/domain/session/    Session 实体、异常和仓储端口
backend/src/domain/mcp/        MCP 配置、发现端口和目录持久化端口
backend/src/domain/tool/       工具定义、安全投影和目录读取端口
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
backend/src/adapter/mcp/                   MCP Server 与工具原子持久化
backend/src/common/            共享 ID 等基础工具
backend/src/infrastructure/    数据库、密码与 MCP transport/秘密加密基础设施
backend/src/main.py            应用组装入口
backend/tests/                 后端测试
backend/migrations/            Alembic 迁移
mcp_servers/                   管理员维护的 stdio MCP Server 源码
docs/architecture.md           架构说明和待决策事项
```

架构说明见 [docs/architecture.md](docs/architecture.md)。
Session 设计见 [docs/session-design.md](docs/session-design.md)。
消息设计见 [docs/message-design.md](docs/message-design.md)。
本工程独立初始化，未复制旧 Athena 的实现。
