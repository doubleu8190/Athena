# Athena v2 系统架构说明

## 1. 系统定位与总体形态

Athena v2 是面向企业内部使用的多用户 AI 智能工作台，采用 **SPA 前端 + 模块化单体 API + 独立异步 Worker + 运维 CLI** 架构：

- **前端工作台（`frontend/`）**：React 18 + TypeScript + Vite + React Router + TanStack Query，经 Nginx（生产）或 Vite Proxy（开发）同源反代 `/api` 与 SSE 流。
- **后端 API（`backend/src/main.py`）**：Python 3.12+ + FastAPI + Uvicorn，提供 REST API、SSE 实时事件流、同源 HttpOnly Cookie 鉴权、前置鉴权与上传准入中间件。
- **独立异步 Worker**：
  - `run_worker.py`：领取排队 Run，执行模型多轮推理、并行工具批次、MCP/Skill/Subagent 编排、上下文压缩与会话自动标题生成。
  - `knowledge_worker.py`（可选）：消费知识库解析与清理任务，使用常驻隔离解析子进程提取文本，经远程 Embedding 写入向量与全文索引，并执行孤儿对象回收。
- **运维 CLI（`backend/src/cli/`）**：提供应急超管初始化（`bootstrap_super_admin.py`）、账号提权（`promote_admin.py`）、飞书组织树全量同步（`sync_feishu_org.py`）及文件孤儿维护（`file_maintenance.py`）。

### 核心基础设施底座

- **PostgreSQL 16+（配合 `pgvector` HNSW）**：作为业务状态、组织树物化路径、行锁并发控制、任务队列、向量检索与 `TSVECTOR` 全文检索的唯一权威持久层。数据库仅依赖 `pgvector` 扩展，**不再依赖 `zhparser` C 扩展**（中文分词在应用侧由 `jieba` 预处理后写入 `TSVECTOR`，历史遗留对象已由迁移 `20261208_0041` 清理）。
- **Redis 7+**：承载 Run SSE 实时事件 Pub/Sub 推送、分布式认证限流与飞书 OAuth 一次性 `state` 原子防重放。`ATHENA_ENV=production` 模式下强制要求配置 `ATHENA_REDIS_URL`。
- **统一单桶对象存储（MinIO / S3，`ATHENA_STORAGE_*`）**：在单一私有桶内通过前缀严格隔离 `knowledge/`、`session-files/`、`session-files-extracted/`、`session-artifacts/`、`skill-logs/`、`skills/` 等业务对象。
- **无状态远程 Embedding 服务**：通过 OpenAI 兼容 HTTP 接口完成向量化（默认 1024 维 `BAAI/bge-m3`），应用与 Worker 进程不加载本地重型向量模型权重。

## 2. 企业身份、组织架构与三层权限

### 2.1 认证与会话边界（开放自助注册已下线）

- **企业登录模式**：
  - **飞书原生 OAuth SSO 与通讯录同步**：支持一键授权登录与全量组织树原子同步；生产环境启用飞书时强制要求配置 Redis（一次性 `state` 防重放）且接口与回调必须为 `https://`。
  - **本地应急超级管理员**：开放自助注册已下线，仅通过 `cli.bootstrap_super_admin` 幂等创建或重置本地 `super_admin` 账号（密码使用 Argon2id 哈希，重置密码同事务吊销全部历史会话）。
  - **非生产本地 Mock 多角色模式**：`ATHENA_AUTH_MOCK_ENABLED=true` 仅限开发与联调，启动时自动幂等预置多部门组织树与 6 个典型角色账号；`ATHENA_ENV=production` 时开启会立即阻断启动。
- **同源 Cookie 会话与实时吊销**：服务端仅保存高熵随机 token 的 SHA-256 摘要，Cookie 为 `HttpOnly`、`SameSite=Lax`（生产须启用 `ATHENA_COOKIE_SECURE=true`），非安全写请求强制校验 `Origin`（配合 `ATHENA_PUBLIC_ORIGIN`）。员工离职同步或管理员停用账号会在同一事务内吊销其全部活跃会话。

### 2.2 三级角色与统一三层权限引擎

- **角色分层**：`super_admin`（全公司超级管理员）、`dept_admin`（部门管理员，通过 `dept_admin_scopes` 管辖指定部门及其子树）与 `member`（普通员工）。
- **组织树与物化路径**：`departments` 表维护物化 `path`（搭配 PostgreSQL `varchar_pattern_ops` 前缀索引），高效支持子树归属与管理范围判定。
- **四条业务线统一隔离**：知识库（`knowledge`）、技能（`skill`）、MCP Server（`mcp`）与子智能体（`subagent`）共享 `app/access`（`AccessPolicyService`）与 `resource_visibility_grants` 授权表，统一落实 `company`（公司公共）/ `department`（部门级 + 精准跨部门/指定人员授权）/ `personal`（个人私有）三层可见性模型；员工调岗后无需重新登录即可在下一次查询或工具执行时实时生效。

## 3. 后端分层与当前模块结构

后端源码位于 `backend/src/`，按职责划分为以下层次：

- `api/`：FastAPI 路由、HTTP/SSE 请求响应 Schema、前置鉴权与上传准入中间件。
- `app/`：业务用例编排、权限策略与后台 Worker 消费逻辑。
- `domain/`：纯领域实体、值对象、业务异常与必要端口定义（不依赖 FastAPI、SQLAlchemy 或外部框架）。
- `adapter/`：SQLAlchemy ORM 模型（共享声明式基类与 `json_column_type()` 位于 `adapter/base.py`，全量聚合在 `adapter/models.py`）、仓储实现与 LLM / Redis / MinIO 适配器。
- `infrastructure/`：数据库连接池、AES-GCM 凭据加密、飞书开放平台客户端、MCP 传输（HTTP / stdio）、有界 HTTP 限流与重试、文档解析及远程 Embedding / Tiktoken 计数驱动。
- `bootstrap/`：API 与各 Worker 进程的组合根装配、生命周期管理及生产环境安全护栏。
- `cli/`：独立运维命令行入口。
- `common/`：跨层共享基础工具（`common.date` 统一提供 `utc_now`/`as_utc`，`common.ids` 提供 `new_id` 与兼容别名 `now_utc`，`common.json` 提供规范序列化与 UTF-8 截断）。

### 当前业务模块清单

| 模块 | 目录（`app/` / `domain/` / `adapter/`） | 核心职责 |
| --- | --- | --- |
| **认证与会话** | `auth` | 本地应急登录、飞书 SSO、非生产 Mock 登录、Cookie 会话、限流与改密 |
| **组织架构** | `org` | 部门树物化路径、企业员工分页查询、部门管理员分权任命、停用吊销、飞书通讯录同步 |
| **统一权限** | `access` | 三层可见性判定（`company`/`department`/`personal`）与 `resource_visibility_grants` 授权管理 |
| **项目目录** | `project` | 项目 CRUD、简介/背景指令/默认技能、关联知识库管理、`detach_sessions=true` 温和删项；主键使用 `new_id("proj")` |
| **对话会话** | `session` | 无项目日常对话与项目绑定会话、关键词搜索（`?q=`）、首条消息前调整归属、软删除行锁 |
| **消息存储** | `message` | 用户可见会话消息流（`conversation_messages`）与内部模型执行流（`execution_messages`）双流隔离持久化 |
| **Run 与编排** | `run` | 异步任务状态机、租约心跳与回收、SSE 增量与数据库对账、澄清/审批/取消/重试、上下文压缩、自动生成会话标题 |
| **轻量偏好与显式记忆** | `preference` | 个人工作背景指令（`user_work_profiles`）与最多 50 条显式备忘记忆（`user_memories`）及 `save_user_memory` 免审批内置工具；**历史重型自动记忆五表与归档 Worker 已于 `20261130_0033` 正式下线** |
| **模型目录与调用** | `llm` | 管理员模型版本、局部保密更新（`PATCH`）、强引用保护软删、场景化普通用户目录（`GET /api/v1/models`）、Run 模型快照固化、压缩策略及 Chat Completions 流式适配器 |
| **系统提示词** | `system_prompt` | 主对话与压缩系统提示词版本化管理与回滚恢复 |
| **MCP 与工具** | `mcp` / `tool` | MCP Server 凭据 AES-GCM 加密与局部保密更新、启停、无损增量重发现、内置工具与 MCP 统一调度、工具中文审批映射、批量审批与业务化待审批卡片 |
| **文件与附件** | `file` | 会话附件上传、文本抽取缓存与预览、多模态图片读取、执行产物安全下载与孤儿清理 |
| **技能系统** | `skill` | 公司/部门级脚本技能包、个人 Markdown Skills、依赖校验与隔离脚本执行 |
| **知识库与检索** | `knowledge` | 三层可见性知识库、多格式文档解析（含可选 PaddleOCR）、无状态远程向量化、`pgvector` HNSW + `jieba`/`TSVECTOR` 混合检索 |
| **子智能体** | `subagent` | 自定义业务型 Subagent 定义、部门可见范围、工具白名单与父子 Run 编排 |

## 4. 关键运行时约束与并发边界

- **数据库行锁边界**：并发一致性完全依赖数据库事务内行锁，不维护跨请求的内存锁对象：
  - Session 创建/改绑项目对目标 Project 行持有 `FOR SHARE`，Project 软删除对 Project 行持有 `FOR UPDATE` 并校验未解绑活跃 Session；
  - Session 更新、软删除、消息追加与 Run 写入按统一锁序锁定 Session 行（`FOR UPDATE`）后再锁 Run 行；
  - 管理写操作（LLM/MCP/组织/Skill/Subagent/系统提示词）在目标写事务内复用无锁单条 `JOIN` 复核函数 `verify_active_session_user` 校验有效会话与当前角色，消除对 `users`/`auth_sessions` 表的跨域行锁争用；仅认证仓储自身改密/改显示名在修改 `users` 行时继续使用 `SqlAlchemyAuthTransactionGuard` 锁定自身行。
- **有界数据库连接池**：`Database` 维护单套有界业务 `AsyncEngine`（`ATHENA_DATABASE_POOL_SIZE`、`ATHENA_DATABASE_MAX_OVERFLOW`）；长耗时 LLM 网络请求、MCP 工具调用、脚本执行与文档解析均在事务之外执行，严禁持锁跨越外部 I/O。
- **服务端可信身份与权限边界**：所有资源归属（`owner_id`）与权限快照均由服务端认证上下文解析，绝不信任客户端或模型提交的用户/部门/知识库标识；跨租户/跨部门越权遵循安全 403/404 约定。
- **数据库迁移单主干**：Alembic 保持单一线性 head（当前为 `20261208_0041`），所有结构与索引变更均需同步维护 ORM 元数据与迁移脚本。

## 5. 前端工作台现状

前端工作台（`frontend/`）已完整落地企业多角色交互闭环：

- **企业登录与会话守卫**：支持飞书 SSO 回调登录、本地应急管理员登录及非生产 Mock 角色一键登录；基于 `/api/v1/auth/me` 与能力接口驱动受保护路由和角色菜单。
- **日常对话工作台**：支持无项目即时对话、场景化模型选择、显式知识库范围勾选、附件上传预览与图片多模态、SSE 实时流式输出与断线数据库对账、停止/重试/补充输入、中文业务化待审批卡片、以及工具与子智能体执行轨迹展示。
- **个人与协作资产中心**：提供项目中心（简介/指令/默认技能/知识库绑定/温和解绑删项）、三层知识库管理与检索调试、个人工作偏好与显式记忆管理、个人 Markdown Skills 管理。
- **分级管理后台**：面向 `super_admin` 与 `dept_admin` 提供组织架构与飞书同步、模型与系统提示词运维、MCP Server 保密运维与批量审批配置、三层技能包管理、Subagent 编排等完整页面。

## 6. 精简架构约定（架构收敛准则）

为避免模块化单体过度抽象导致样板代码与空壳目录膨胀，后续新增功能与重构必须遵守以下收敛准则（同时固化于仓库根目录 `AGENTS.md`）：

1. **按需抽象端口，拒绝默认 1:1 样板 `Protocol`**：不再为只有一个生产实现（且无多后端切换需求）的模块默认新建 1:1 `Protocol` 端口；仅在确实存在多个生产实现（如多种存储/模型/认证后端）或明确需要复杂领域边界隔离时引入抽象接口。
2. **精简应用层中间转换与叶子子包**：不再为简单模块默认创建 `app/<module>/{model,converter}/result.py` 中间结果转换层，也不创建仅含单个文件的叶子子包（如仅有一个文件的 `model/`、`converter/`、`repository/` 子目录）；可直接由应用服务返回清晰的领域对象或轻量 dataclass，并在模块顶层使用扁平单文件（如 `model.py`、`repository.py`、`converter.py`）。
3. **复用跨模块共享基础工具**：时间规范化统一复用 `common.date.as_utc` / `utc_now`，跨方言 JSON 列统一复用 `adapter.base.json_column_type()`，有界 HTTP 限流与重试统一复用 `infrastructure.http`，禁止在各业务模块间复制粘贴同类辅助函数或保留无意义的 re-export 垫片。
4. **保持模块边界与持久化归属清晰**：精简目录层级不改变模块数据归属原则——业务模块之间通过明确的应用服务或只读共享查询接口协作，不绕过目标模块的事务与行锁约定直接篡改其持久化状态。
