# Athena v2 — 面向多用户的通用 AI 智能助手

<p align="center">
  🏛️ <strong>Athena v2</strong> 🏛️
</p>

<p align="center">
  <strong>基于模块化单体与异步 Worker 架构的多用户 AI Agent 平台</strong>
</p>

<p align="center">
  <a href="#-快速开始">快速开始</a> •
  <a href="#-功能概览">功能概览</a> •
  <a href="#-技术架构">技术架构</a> •
  <a href="#-配置说明">配置说明</a> •
  <a href="#-api-与项目文档">API 与文档</a> •
  <a href="#-生产部署">生产部署</a>
</p>

---

## 📖 项目简介

Athena v2 是一个面向多用户的通用 AI 智能助手平台，采用 **SPA 前端 + 模块化单体 API + 独立异步 Worker** 架构，以 PostgreSQL（`pgvector` + `zhparser`）作为核心状态持久化与混合检索底座，按需扩展 Redis、MinIO 以及外部模型与工具生态。

### 核心设计理念

- **计算与入口解耦**：API 专注鉴权、调度与流式推送，长耗时推理、记忆提取与文档解析由独立 Worker 异步消费
- **Human-in-the-Loop (HITL)**：支持工具调用的运行期逐项审批与补充输入，确保关键操作可控、可恢复
- **PostgreSQL 一体化底座**：统一承载业务状态、行锁并发控制、异步任务队列以及向量与中文全文检索
- **可插拔能力扩展**：通过管理后台动态维护模型、MCP、Skills 与 Subagent，按需开启长期记忆与知识库
- **实时可观测性**：基于 SSE 推送增量输出、工具调用轨迹与执行状态，断线后可基于持久化快照可靠对账

---

## ✨ 功能概览

### 🤖 Agent 执行与编排
- **异步 Run 执行循环**：支持流式回答、并行工具批次、停止取消、补充输入与失败重试
- **模型与提示词管理**：管理员配置不可变模型版本、系统提示词版本与上下文压缩策略
- **自定义 Subagent**：支持按业务场景编排子代理并限定可用工具白名单

### 🔧 工具与技能生态
- **全局 MCP 集成**：支持 HTTP 与 stdio MCP Server 注册发现、加密凭据管理与安全工具目录
- **Skills 与会话文件**：支持平台脚本技能包、个人 Markdown Skills 以及会话附件/产物隔离存储

### 📚 知识库与长期记忆
- **多格式知识库 RAG**：支持文本与常见文档/OCR 异步解析、向量与中文全文混合检索及项目关联
- **全生命周期长期记忆**：自动从终态对话提取记忆并执行合并决策、按需召回与周期归档清理

### 🔒 多用户与安全审批
- **同源认证与角色隔离**：邮箱密码认证、服务端可撤销 HttpOnly Cookie 会话、同源校验与 RBAC 管理权限
- **运行期工具审批**：支持按工具维度配置审批策略，命中审批的批次暂停等待确认后原子恢复执行

---

## 🏗️ 技术架构

### 整体架构设计

```mermaid
flowchart TB
    subgraph UI["用户界面与网关层 (frontend)"]
        SPA["React 18 + TypeScript SPA"]
        Proxy["Nginx 静态托管 / 开发期 Vite 代理"]
    end

    subgraph Gateway["应用网关层 (backend-api / FastAPI)"]
        Middleware["前置鉴权与上传准入中间件"]
        RestApi["REST API 与管理接口"]
        SseStream["SSE 实时事件流"]
    end

    subgraph Workers["异步执行层 (Async Workers)"]
        RunWorker["run_worker (对话与工具执行)"]
        MemWorker["memory_worker (长期记忆, 可选)"]
        KbWorker["knowledge_worker (文档解析与索引, 可选)"]
    end

    subgraph Ext["外部能力与扩展层"]
        LLM["LLM Providers (Chat Completions)"]
        MCP["MCP Servers (HTTP / stdio)"]
        SkillSubagent["Skills 脚本运行时 / Subagent 编排"]
        LocalModels["本地 BGE / OCR 模型 (按需)"]
    end

    subgraph Infra["数据与基础设施层"]
        PG[("PostgreSQL (pgvector + zhparser)")]
        Redis[("Redis (Pub/Sub, 可选)")]
        MinIO[("MinIO (私有 S3 桶, 按需)")]
    end

    SPA --> Proxy
    Proxy -->|"/api & SSE"| Middleware
    Middleware --> RestApi
    Middleware --> SseStream

    RestApi --> PG
    SseStream --> PG
    SseStream -.->|"事件订阅"| Redis
    RestApi -.->|"文件 / 知识库"| MinIO
    RestApi -.->|"工具发现"| MCP
    RestApi -.->|"检索向量"| LocalModels

    RunWorker --> PG
    RunWorker -.->|"增量推送"| Redis
    RunWorker -.->|"文件 / 知识库"| MinIO
    RunWorker --> LLM
    RunWorker -.->|"工具调用"| MCP
    RunWorker -.-> SkillSubagent
    RunWorker -.-> LocalModels

    MemWorker --> PG
    MemWorker --> LLM
    MemWorker --> LocalModels

    KbWorker --> PG
    KbWorker --> MinIO
    KbWorker --> LocalModels
```

> 实线为核心依赖路径，虚线为可选或按配置启用的扩展路径；API 与各 Worker 通过 PostgreSQL 队列解耦，并可选经 Redis Pub/Sub 推送实时 SSE 增量。

### 后端代码分层

后端采用模块化单体与 DDD 分层架构：`api`（HTTP/SSE 入口与鉴权/上传准入中间件）→ `app`（用例编排）→ `domain`（领域实体、事件与仓储端口）← `adapter`（ORM 仓储与外部适配）/ `infrastructure`（数据库、加密安全与底层驱动），由 `bootstrap` 组合根按进程统一装配，辅以 `cli`（运维命令）与 `common`（共享工具）。

### 技术栈

| 层级 | 技术选型 | 职责 |
| --- | --- | --- |
| **前端界面** | React 18 + TypeScript + Vite + React Router + TanStack Query | 多用户 SPA 应用、SSE 实时渲染与状态缓存 |
| **网关与托管** | Nginx（生产）/ Vite Proxy（开发） | 静态资源托管、同源 `/api` 与 SSE 反向代理 |
| **后端 API** | Python 3.12+ + FastAPI + Uvicorn + Pydantic | REST API、SSE 推送、同源 Cookie 鉴权与上传准入 |
| **持久化与检索** | PostgreSQL + SQLAlchemy (asyncio) + Alembic + `pgvector` + `zhparser` | 业务状态、行锁并发、异步任务队列、向量与中文全文检索 |
| **实时事件通道** | Redis (`redis-py`, 可选) | Run 增量事件 Pub/Sub，不可用时自动降级为数据库对账 |
| **对象存储** | MinIO (`minio`, 按需) | 独立私有桶分别存储知识库原件与 Skills/会话文件 |
| **模型与工具生态** | OpenAI SDK (Chat Completions) + MCP SDK + `sentence-transformers` + `tiktoken` | 外部模型调用、HTTP/stdio MCP 工具、本地 BGE 向量/重排与 token 计数 |
| **文档解析 (可选)** | PaddleOCR + `pypdfium2` + `python-docx` / `openpyxl` / `python-pptx` + `jieba` | PDF / Office / 图片多格式文本与 OCR 提取 |
| **容器与 CI** | Docker Compose + GitHub Actions (`pytest` / `ruff` / `mypy` / `eslint` / `vitest` / `playwright`) | 本地测试栈、生产容器编排与自动化质量门禁 |

---

## 📁 项目结构

```text
Athena_v2/
├── .github/workflows/          # GitHub Actions CI 工作流 (ci.yml)
├── backend/
│   ├── alembic.ini             # Alembic 迁移配置
│   ├── migrations/             # PostgreSQL 数据库迁移脚本
│   ├── src/                    # 后端源码（模块化单体）
│   │   ├── api/                # HTTP/SSE 路由、DTO、鉴权与上传准入中间件
│   │   ├── app/                # 应用服务、用例编排与 Worker 消费逻辑
│   │   ├── domain/             # 领域实体、值对象、异常与仓储端口
│   │   ├── adapter/            # SQLAlchemy 仓储与 LLM / Redis / MinIO 适配
│   │   ├── infrastructure/     # 数据库连接、加密安全、MCP 与解析/向量驱动
│   │   ├── bootstrap/          # API 与各 Worker 进程的依赖装配组合根
│   │   ├── cli/                # 运维 CLI（管理员提权、文件离线维护）
│   │   ├── common/             # 共享基础工具（ID、时间等）
│   │   ├── main.py             # FastAPI 应用入口
│   │   ├── run_worker.py       # Run 异步执行 Worker 入口
│   │   ├── memory_worker.py    # 长期记忆生命周期 Worker 入口
│   │   └── knowledge_worker.py # 知识库解析与索引 Worker 入口
│   └── tests/                  # 单元测试与 PostgreSQL / Redis / MinIO 集成测试
├── frontend/
│   ├── src/
│   │   ├── app/                # 应用入口、Providers、路由与受保护外壳
│   │   ├── features/           # 业务域（auth/chat/projects/sessions/knowledge/skills/files/admin）
│   │   ├── pages/              # 跨域通用页面
│   │   ├── shared/             # API 客户端、通用 UI 组件与工具库
│   │   └── test/               # Vitest 测试工具与 fixtures
│   └── e2e/                    # Playwright 浏览器 E2E 测试
├── mcp_servers/                # 随镜像构建部署的本地 stdio MCP Server 源码
├── docker/                     # Dockerfile、Nginx 网关配置、依赖镜像与测试模板
├── scripts/                    # Docker 测试栈启停、凭据初始化与环境加载脚本
├── docs/                       # 架构、功能设计与运维手册文档
├── compose.yml                 # 生产环境 Docker Compose 编排配置
├── compose.test.yml            # 本地测试依赖（PostgreSQL、Redis、MinIO）编排配置
├── pyproject.toml              # Python 依赖、打包元数据与 Ruff / Mypy / Pytest 配置
└── .env.example                # 全量环境变量模板与生产安全自检清单
```

---

## 🚀 快速开始

### 环境要求

- **Python**: `>= 3.12`（见 [`pyproject.toml`](pyproject.toml)；如需启用知识库 OCR，请勿使用 Python 3.14）
- **Node.js**: `>= 20` 与 npm（见 [`frontend/README.md`](frontend/README.md)）
- **Docker**: Docker Desktop 或 Docker Engine（用于本地测试容器栈或容器部署）

### 方式一：Docker 一键启动本地测试栈

`docker/test.env` 与 `secrets/` 专用于 Docker 测试栈，与宿主机 `.env` 职责分离，不要互相复制。启动后默认访问 `http://localhost:8080`。

```bash
# 1. 初始化本地测试配置与凭据
[ -f docker/test.env ] || cp docker/test.env.example docker/test.env
scripts/bootstrap-test-secrets.py --fresh  # 仅全新环境；已有旧数据或旧配置去掉 --fresh

# 2. scripts/test-docker.sh 常用命令
scripts/test-docker.sh                     # 默认 up：拉起依赖、构建镜像、迁移并启动全栈
scripts/test-docker.sh up --build-base     # 强制重建后端基础依赖镜像并启动
scripts/test-docker.sh status              # 查看业务与基础设施容器状态
scripts/test-docker.sh logs                # 实时跟踪业务容器日志
scripts/test-docker.sh down --all          # 停止业务与基础设施容器（保留 data/ 持久化数据）
```

### 方式二：宿主机本地开发

以下命令在仓库根目录执行。

#### 1. 安装依赖并初始化 `.env`

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'       # 启用知识库解析需改为 '.[dev,knowledge]'
npm --prefix frontend install
cp .env.example .env && chmod 600 .env

# 生成 32 字节 Base64 主密钥填入 .env 的 ATHENA_MCP_SECRET_KEY，并配置 ATHENA_DATABASE_URL
python3 -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())'
```

#### 2. 加载 `.env` 并执行数据库迁移

```bash
set -a && source .env && set +a
.venv/bin/alembic -c backend/alembic.ini upgrade head

# 如需清空并重建本地开发数据库（可选）
.venv/bin/alembic -c backend/alembic.ini downgrade base
.venv/bin/alembic -c backend/alembic.ini upgrade head
```

#### 3. 启动后端 API、前端与后台 Worker

后端与 Worker 进程不会自动读取 `.env`，新开终端需先执行 `set -a && source .env && set +a`：

```bash
# 后端 API（终端 1）
.venv/bin/python -m uvicorn main:create_app --factory

# 前端开发服务器（终端 2）
cd frontend && npm run dev

# 后台 Worker（按需另开终端启动）
.venv/bin/python -m run_worker         # 必选：对话 Run 与工具执行
.venv/bin/python -m memory_worker      # 可选：长期记忆生命周期
.venv/bin/python -m knowledge_worker   # 可选：知识库文档解析与索引
```

#### 4. 访问应用与首个管理员提权

- **前端界面**：`http://localhost:5173`（Vite 开发服务器自动代理 `/api` 到 `127.0.0.1:8000`，保持同源 Cookie 校验）
- **后端 API / 存活探针 / OpenAPI 文档**：`http://127.0.0.1:8000` / `/health` / `/docs`
- **首个管理员提权**（先注册普通账号，再在已加载 `ATHENA_DATABASE_URL` 的仓库根目录执行；不会自动创建账号或设置密码）：

```bash
.venv/bin/python -m cli.promote_admin --email <email>
```

---

## ⚙️ 配置说明

复制 [`.env.example`](.env.example) 为 `.env` 按需配置。模型版本、供应商 API Key 与上下文压缩策略均通过管理员后台动态管理，并由 `ATHENA_MCP_SECRET_KEY` 加密落库，无需通过环境变量注入模型密钥。

```bash
# 核心必填（PostgreSQL 需启用 pgvector 与 zhparser 扩展）
ATHENA_DATABASE_URL=postgresql+asyncpg://user:password@127.0.0.1:5432/athena
ATHENA_MCP_SECRET_KEY=<32 字节 Base64 随机主密钥>

# 同源与认证 Cookie（HTTPS 生产环境须设 ATHENA_COOKIE_SECURE=true 与公网规范 Origin）
ATHENA_COOKIE_SECURE=false
ATHENA_PUBLIC_ORIGIN=http://127.0.0.1:8000

# 可选扩展开关（详见 .env.example）
ATHENA_REDIS_URL=redis://127.0.0.1:6379/0
ATHENA_MEMORY_ENABLED=false
ATHENA_MEMORY_LIFECYCLE_ENABLED=false
ATHENA_FILES_ENABLED=false
ATHENA_KNOWLEDGE_ENABLED=false
```

> 完整环境变量、统一对象存储参数（`ATHENA_STORAGE_*`）、远程 Embedding 服务（`ATHENA_EMBEDDING_*`）、Worker 调优与生产上线安全自检清单见 [`.env.example`](.env.example)。

---

## 🧪 测试与检查

### 后端单元测试与静态检查

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check backend/src backend/tests
.venv/bin/python -m mypy --strict backend/src
```

### 后端集成测试

需先完成测试栈凭据初始化并启动 [`compose.test.yml`](compose.test.yml) 中的 PostgreSQL、Redis 与 MinIO（或先运行 `scripts/test-docker.sh`）；PostgreSQL 测试会创建并在结束后删除独立临时 schema，不影响业务数据：

```bash
docker compose --env-file docker/test.env -f compose.test.yml up -d
source scripts/load-test-env.sh
.venv/bin/python -m pytest -m integration backend/tests/integration
```

### 前端检查与构建

```bash
cd frontend
npm run typecheck
npm run lint
npm run test
npm run build
```

---

## 📚 API 与项目文档

### REST API 与 SSE

启动后端后访问 `http://127.0.0.1:8000/docs` 查看完整 OpenAPI（Swagger）文档。主要模块路由前缀如下：

| 模块 | 路由前缀 / 端点 | 说明 |
| --- | --- | --- |
| **健康检查** | `GET /health` | 进程存活探针 |
| **认证与账户** | `/api/v1/auth/*` | 注册、登录、登出、个人资料与改密 |
| **项目与会话** | `/api/v1/projects`, `/api/v1/sessions` | 项目与会话 CRUD、消息历史（`/api/v1/sessions/{session_id}/messages`） |
| **Run 与 SSE** | `/api/v1/sessions/{session_id}/runs` | 提交、取消、重试、补充输入、工具审批、轨迹与 SSE（`/{run_id}/stream`） |
| **文件与技能** | `/api/v1/sessions/{session_id}/files`, `/api/v1/skills`, `/api/v1/me/skills` | 会话文件上传/下载、可用技能目录与个人 Markdown Skills |
| **知识库与检索** | `/api/v1/knowledge-bases`, `/api/v1/projects/{project_id}/knowledge-bases` | 知识库、文档版本、混合检索与项目关联 |
| **公共工具目录** | `/api/v1/tools` | 已认证用户可见的安全工具目录查询 |
| **模型与提示词管理** | `/api/v1/admin/llm/*`, `/api/v1/admin/system-prompts` | 模型版本、压缩策略与系统提示词版本（管理员） |
| **生态扩展管理** | `/api/v1/admin/mcp/servers`, `/api/v1/admin/tools`, `/api/v1/admin/skills`, `/api/v1/admin/subagents` | MCP 发现、工具审批策略、技能包导入与 Subagent 编排（管理员） |

### 核心文档

| 文档 | 说明 |
| --- | --- |
| [`.env.example`](.env.example) | 全量环境变量说明与生产安全自检清单 |
| [`docs/architecture.md`](docs/architecture.md) | 架构起点与模块边界 |
| [`frontend/README.md`](frontend/README.md) | 前端架构、路由、认证边界与开发说明 |
| [`mcp_servers/README.md`](mcp_servers/README.md) | 本地 stdio MCP Server 约定与镜像构建 |
| [`docs/knowledge-base-operations.md`](docs/knowledge-base-operations.md) | 知识库运行环境、MinIO 与 OCR 运维手册 |
| [`docs/skills-files-operations.md`](docs/skills-files-operations.md) | Skills 与会话文件运行手册及离线维护 |
| [`docs/run-design.md`](docs/run-design.md) | Run 执行模型与状态机设计 |
| [`docs/tools-mcp-design.md`](docs/tools-mcp-design.md) | 全局 MCP 与工具管理设计 |
| [`docs/memory-lifecycle-design.md`](docs/memory-lifecycle-design.md) | 长期记忆提取、检索与生命周期设计 |

---

## 📦 生产部署

参考 [`compose.yml`](compose.yml) 与 [`.env.example`](.env.example)，复制 `.env.example` 为 `.env.prod`（`chmod 600 .env.prod`）并填写外部 PostgreSQL、Redis、MinIO 及主密钥配置。HTTPS 生产环境必须设置 `ATHENA_COOKIE_SECURE=true` 并配置规范公网 `ATHENA_PUBLIC_ORIGIN`。

```bash
# 1. 预构建后端基础依赖镜像（如需启用知识库 OCR，加 --build-arg INSTALL_KNOWLEDGE=true）
docker build -f docker/Dockerfile.backend-base -t athena-backend-base:latest .

# 2. 执行数据库迁移
docker compose --env-file .env.prod run --rm backend-migration

# 3. 启动生产服务（如需启动知识库 Worker，在 up 前加 --profile knowledge）
docker compose --env-file .env.prod up -d

# 4. 注册首个账号后执行管理员提权
docker compose --env-file .env.prod run --rm backend-api python -m cli.promote_admin --email <email>
```
