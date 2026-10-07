# Athena 重构迁移区

这是 Athena 的唯一生产实现，采用 Domain、Application、Infrastructure、Interfaces 和 Bootstrap 分层。

项目使用 `backend` Python 包名，入口、测试和发布配置都位于本目录。

## 目标

依赖方向固定为：

```text
interfaces -> application -> domain
interfaces -> application ports
application -> domain ports
infrastructure -> domain/application ports
bootstrap -> interfaces + application + infrastructure
```

`domain` 不依赖 FastAPI、SQLAlchemy、LangGraph、PostgreSQL、Neo4j、pgvector、Docker 或具体 LLM SDK。

## 目录

- `src/interfaces`：HTTP Controller、DTO、认证、中间件和 SSE。
- `src/application`：面向用例的 Service，负责业务编排和事务边界。
- `src/domain`：领域实体、值对象、领域规则、事件和端口。
- `src/infrastructure`：数据库、ORM、向量库、图数据库、LLM、沙箱、MCP 和 LangGraph 适配器。
- `src/workers`：命令消费者、文档处理、记忆任务等后台进程入口。
- `src/bootstrap`：应用启动、生命周期和依赖注入组装。
- `src/shared`：跨域且无业务含义的基础工具。
- `tests`：新结构的单元、集成和架构边界测试。

迁移顺序和每阶段验收标准见 [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md)，依赖约束见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

阶段 0 基线见 [`BASELINE.md`](BASELINE.md)。最小启动检查：

```bash
.venv/bin/python -m backend.src.bootstrap.check

生产入口：`PYTHONPATH=. .venv/bin/python -m backend.src.main`。
```
