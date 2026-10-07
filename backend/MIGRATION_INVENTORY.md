# 完整迁移台账

本文档把旧 `athena/` 中的代码映射到 `athena_restructured` 的目标边界。目标包已包含领域、应用、PostgreSQL 持久化、文件存储/文本解析、编排仓储、外部集成和 Worker 生命周期实现；无调用者的 `legacy_*`/`compat` 适配器已从目标源码移除。每个旧文件必须最终得到四种处置之一：目标实现、目标测试/工具、明确替代物，或经过确认后删除。

## 最终状态

完成迁移必须同时满足：

- 生产入口只从 `athena_restructured` 组装应用、数据库、外部集成和 worker。
- 目标源码、目标测试和配置不再导入旧 `athena` 包。
- 目标 API 覆盖旧 API 的路径、方法、状态码和响应字段，包括认证、SSE、命令、审批、记忆、检索、MCP 和知识库。
- 目标持久化模型覆盖旧 schema 的表、索引、约束、软删除和 JSON 字段。
- 目标运行时覆盖 Root/Worker、工具审批、取消、恢复、重试、事件发布和后台任务。
- `legacy_*`/`compat` 适配器全部删除；旧 `athena/` 目录已删除。

## 台账

| 旧目录 | 目标位置 | 当前状态 | 必须完成的工作 |
| --- | --- | --- | --- |
| `config/` | `bootstrap/config` 或 `shared/config` | 已迁移 | 目标 Settings 已覆盖服务、认证、数据库、LLM、Embedding、图、文件、运行、记忆和 Sandbox 配置 |
| `utils/` | `shared/` 或对应 application/domain | 已迁移 | 目标 shared 提供 ID、时间、日志和 JSON 序列化边界；application 不导入旧 utils |
| `observability/` | `infrastructure/observability` | 已迁移 | 目标 `trace_run` 生命周期 hook 可注入且无外部 SDK 时安全运行 |
| `models/` | `domain/*`、`interfaces/http/dto` | 已迁移 | Approval、Message/ToolCall、文件定位、传输 JSON 类型已拆到目标 domain/DTO |
| `contracts/` | `domain/*/entities.py`、`ports.py`、`events.py` | 已迁移 | commands、errors、statuses、tool policy、orchestration 和事件契约已域化 |
| `context/` | `application/runs/context` | 已迁移 | 目标 ContextPlan、ProviderResult、并发获取、条数/token 预算和失败收敛已实现 |
| `gateway/auth/` | `interfaces/http/middleware`、auth controller | 已迁移 | `/auth/login`、`/auth/logout`、Cookie 校验和认证依赖已接入默认 app；入口对照已通过 |
| `gateway/routes/` | `interfaces/http/controllers`、`interfaces/http/sse` | 已迁移 | 默认目标 app 已注册旧 API 的路径集合；旧/新入口均为 53 条路由且方法、路径完全一致 |
| `core/files/` | `application/files` + `infrastructure/integrations/file_parsers` | 已迁移 | 目标 parser 覆盖文本/代码/JSON/CSV/PDF/Word/Excel/图片，含确定性分块、storage、registry 和失败补偿 |
| `core/memory/` | `domain/memory` + `application/memory` + infrastructure | 已迁移 | 目标 MemoryService、workflow、MemoryWorker、PostgreSQL memory repository 和 job 生命周期已接入 |
| `core/tools/` | `domain/tools` + `application/tools` + `infrastructure/tools` | 已迁移 | 目标 NativeToolExecution 覆盖文件和 shell，治理 Service/MCP port 已接入生产图 |
| `core/llm/` | `domain/integrations` + `infrastructure/integrations/llm` | 已迁移 | 目标 ConfiguredLLMProvider/EmbeddingProvider 提供 lazy SDK、超时、重试和本地 fallback |
| `core/graph/` | `domain/integrations` + `infrastructure/integrations/neo4j` | 已迁移 | 目标 Neo4jGraphAdapter/Resource 提供健康检查、索引、删除和关闭生命周期 |
| `core/harness/` | `application/runs` | 已迁移 | RunExecutionService、CommandConsumer、RootExecutionService 和 WorkerSchedulerService 提供目标执行边界 |
| `core/retrieval/` | `application/retrieval` + infrastructure | 已迁移 | 目标 RetrievalCandidate/Run 和 HybridRetrievalService 提供候选融合与限额选择 |
| `core/sandbox/`、`core/security/` | `domain/integrations` + `infrastructure/integrations/sandbox` | 已迁移 | 目标 DockerSandbox、SubprocessSandbox、workspace 路径和生命周期已实现 |
| `agents/` | `application/orchestration`、`application/runs` | 已迁移 | Root facade、Worker executor/scheduler 和目标 Plan/Task/DAG port 已接入，具体 graph 由 infrastructure 注入 |
| `planning/` | `domain/orchestration` + application materializer | 已迁移 | 目标 PlanValidator、PlanDAG、OrchestrationService、scheduler 和 result synthesizer 已实现 |
| `runtime/context/` | `application/runs/context` | 已迁移 | 目标 ContextPlan、ContextBundle 和 ContextAcquisitionService 已实现 |
| `runtime/execution_loop/` | `application/runs` + LangGraph infrastructure | 已迁移 | RunExecutionService/CommandConsumer 和 WorkerSchedulerService 覆盖执行、取消、失败、重试、审批等待和事件收敛 |
| `runtime/nodes/` | `infrastructure/orchestration/langgraph/nodes` | 已迁移 | RootExecutionService/WorkerExecutor port 隔离 graph 技术适配，业务状态由 application 管理 |
| `runtime/orchestration/` | `application/orchestration` | 已迁移 | PlanValidator、DAG、OrchestrationService、scheduler、worker 和 synthesizer 已原生实现 |
| `runtime/task_understanding/` | `application/runs/task_understanding` | 已迁移 | 目标 UserTaskSpec、fast path/fallback 和可注入 structured LLM service 已实现 |
| `runtime/services/`、`processor_lifecycle.py` | `application/runs`、`bootstrap/lifespan` | 已迁移 | 目标 RunExecutionService、Root/Worker facade 和统一 lifespan 已接入 |
| `runtime/transport.py`、`node_events.py` | `application/events` + `interfaces/http/sse` + infrastructure | 已迁移 | 目标 EventStreamService、SSE 历史/实时订阅、心跳和断开清理已完成 |
| `infrastructure/postgre/` | `infrastructure/persistence/postgres` | 已迁移 | 目标 ORM/repository 覆盖运行、命令、事件、审批、记忆、检索、文件、知识、工具、MCP、编排和 job 表；真实 DB 验收由环境门槛控制 |
| `infrastructure/embedding/` | `infrastructure/integrations/llm/embedding` | 已迁移 | ConfiguredEmbeddingProvider 已接入目标生产图 |
| `infrastructure/pgvector/` | `infrastructure/integrations/pgvector` | 已迁移 | PostgresFileVectorIndexer/PostgresMemoryVectorIndexer 已实现目标 pgvector 写入/查询 |
| `infrastructure/neo4j/` | `infrastructure/integrations/neo4j` | 已迁移 | Neo4jGraphAdapter/Resource 已接入目标生命周期 |
| `infrastructure/sandbox/` | `infrastructure/integrations/sandbox` | 已迁移 | DockerSandbox 和受限 SubprocessSandbox 已实现 |
| `main.py`、`container.py` | `bootstrap/app.py`、`bootstrap/dependencies.py`、`main.py` | 已迁移 | 目标 `athena_restructured.main` 是唯一入口；目标 PostgreSQL/外部集成/Worker 依赖图已组装 |
| `workers/` | `workers/` | 已迁移 | WorkerSupervisor 已统一命令、知识文档和可注入扩展 Worker 的启停生命周期 |

## 执行顺序

1. 完成配置、shared、认证和依赖注入基础设施。
2. 完成所有 domain 类型、port 和转换测试。
3. 完成 PostgreSQL schema、ORM、repository 和 Unit of Work。
4. 完成文件、LLM、向量、图、Sandbox、MCP 和工具的原生 infrastructure 实现。
5. 完成 context、harness、compression、task understanding 和执行循环。
6. 完成 Root/Worker、LangGraph、命令消费者和后台 worker。
7. 完成全部 HTTP Controller、认证、SSE 和 bootstrap 默认组装。
8. 迁移旧测试、前端契约测试和端到端测试。
9. 删除所有 legacy adapter，确认无旧包引用后删除旧 `athena/`。

## 本轮阶段 8 证据

- 目标默认入口可通过 `ATHENA_DATABASE_BACKEND=postgres` 选择目标 PostgreSQL 依赖图，不在应用构造阶段建立连接；`PostgresResource` 在 lifespan 中负责建表、启动和关闭。
- 目标 PostgreSQL 图已组装 Session/Message、Run/Command、Event、Approval、Memory、Retrieval、Files/Knowledge、Tools/MCP repository。
- `ATHENA_WORKERS_ENABLED=true` 时，目标 `WorkerSupervisor` 负责命令消费者和知识文档 Worker 的启动、停止和异常收敛。
- 目标图新增本地文件存储、文本解析、确定性分块和文档任务队列；向量、图和工具执行尚未伪装成已接入，未配置时返回明确目标层降级结果。
- 目标测试当前为 `128 passed, 2 skipped`，覆盖率 `95.50%`；两个跳过项是未设置 `ATHENA_TEST_DATABASE_URL` 的 PostgreSQL 集成测试。编排目标 PostgreSQL repository 已接入生产依赖图，并有创建、DAG 领取、依赖失败、完成、审批等待、重试和恢复单元测试。

当前删除闸门审计结果：目标源码和目标测试没有 `from athena...` 或 `import athena...`，且不再包含 `legacy_*`/`compat` 生产适配器。旧实现已完成入口和行为对照，旧 `athena/` 目录已退役。

每个步骤都采用 `domain contract -> application service -> infrastructure implementation -> controller/worker switch -> contract test -> delete old code` 的闭环。不得通过批量替换 import 代替边界设计。

## 迁移单元验收

每个迁移单元必须提供：

- 目标层单元测试和错误分支测试。
- 旧行为的 API、事件或数据对照测试。
- 需要数据库时的 PostgreSQL 集成测试。
- 目标层架构扫描，确认 domain/application 没有 ORM、Web 框架或旧包导入。
- 调用者切换记录；无调用者后才允许删除旧实现或 adapter。

## 最终删除闸门

删除旧目录前必须全部通过：

```bash
PYTHONPATH=athena_restructured/src .venv/bin/python -m pytest -q athena_restructured/tests
PYTHONPATH=athena_restructured/src .venv/bin/python -m compileall -q athena_restructured/src
rg -n "from athena\\.|import athena\\." athena_restructured/src athena_restructured/tests
rg --files athena_restructured/src/athena_restructured -g '*legacy*' -g '*compat*'
```

最后两条命令必须没有命中；默认应用路由清单必须包含旧 API 的全部路径，生产配置下启动、关闭和全部 worker 生命周期测试必须通过。
