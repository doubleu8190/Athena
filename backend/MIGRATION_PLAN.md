# 渐进式迁移顺序

迁移采用“新增目标实现、双轨验证、逐入口切换、删除旧实现”的方式。除最后切换阶段外，不移动或删除当前 `athena/` 文件；每一阶段完成后旧入口仍必须可启动。

## 阶段 0：基线和脚手架

状态：已完成基线记录、独立包入口、最小启动探针和架构违规检测；旧入口仍保持不变。

- 固定当前 API、SSE、命令、数据库表和后台任务的行为基线。
- 为新包建立独立 import path 和最小启动检查。
- 加入架构测试，检查禁止的反向依赖。
- 记录当前工作树已有修改，不将其回退或覆盖。

验收：迁移区测试通过；`athena_restructured` 可以独立 import；启动探针成功；架构测试能发现一次故意违反依赖的示例。旧项目全量测试结果记录在基线中，不因阶段 0 的脚手架修改而重写。

## 阶段 1：共享类型、错误和端口

阶段 1 按业务域拆分契约，不把旧的全局 `contracts/events.py` 整体复制到新 domain。当前工作单元是 Session/Message；应用事件、运行状态和文件/工具契约分别在后续工作单元处理。

当前状态：已完成。Session/Message、Files/Knowledge、Tools/MCP、Runs、Application events、Approval 和 Memory 的领域契约、port 及转换边界均已建立；迁移区内的适配器通过注入对象和目标包 DTO 通信，不依赖目录外 `athena` 包。

工作单元顺序：

1. Session/Message：实体、角色/状态、消息嵌套值对象、仓储端口。
2. Files：附件引用、附件状态、文件定位和元数据值对象。
3. Runs/Approval：命令、运行、审批状态和领域错误。
4. Application events：领域事件与应用事件分离；SSE 投影契约留在 interfaces/infrastructure 边界。
5. Tools/Knowledge/Memory：各自的领域策略和值对象。

每个工作单元只允许新增目标类型和转换函数，不修改旧调用者；完成后再由对应 Service 或 Controller 使用新 port。

阶段 1 与阶段 2 按业务域设置闸门，而不是要求整个阶段 1 一次性完成：某个域完成实体、值对象、port 和旧类型转换后，可以进入该域的持久化切片；其他域继续留在阶段 1。当前 Session/Message、Files/Knowledge、Tools/MCP 已达到这个闸门。

Session/Message 工作单元验收状态：

- [x] Session 保留旧模型中的压缩字段。
- [x] Message、MessageRole、ToolCall、AttachmentRef 使用无框架依赖的领域类型。
- [x] SessionRepository、MessageRepository 按业务域定义。
- [x] 旧 Session/Message repository 有明确转换适配器。
- [x] 领域边界测试禁止 Pydantic 和旧 `athena` 导入。
- [x] Message 查询 Service 和 Controller 接入（阶段 3 低风险查询迁移）。
- [x] Session CRUD 查询 Service 和 Controller 接入（阶段 3 第一个切片）。

Files/Knowledge、Tools/MCP 工作单元契约状态：

- [x] Files/Knowledge 实体和值对象无框架依赖。
- [x] Files/Knowledge repository port 和旧类型转换测试已建立。
- [x] Tools/MCP 实体和值对象无框架依赖。
- [x] Tools/MCP repository port 和旧类型转换测试已建立。
- [x] Runs 命令、运行摘要和命令存储 port 已在阶段 5 首个切片迁移。
- [x] Application events 契约、事件持久化和发布 port 已在阶段 5 迁移。
- [x] Approval 和 Memory 契约已在阶段 7 的目标 domain/application 中迁移。

阶段 1 的长期迁移范围包括：

- `contracts/statuses.py`、`contracts/errors.py`、`contracts/events.py` 中可复用的枚举和事件。
- `models/session.py`、`models/message.py`、`models/file.py`、`models/tool.py` 中的领域实体和值对象，按业务域拆开。
- `contracts/ports.py` 按域拆成 `domain/*/ports.py`，不保留一个新的全局 port 门面。
- `utils/id_generation.py`、时间、日志等无业务工具移到 `shared`。

验收：domain 包不导入旧 `athena`、`gateway`、`runtime`、`infrastructure` 或 Pydantic；旧代码继续使用旧类型；每个已迁移类型都有明确转换函数和单元测试。

## 阶段 2：持久化适配器和转换边界

状态：已完成。Session/Message、Files/Knowledge、Tools/MCP 三个域的持久化边界均已实现，并已通过真实 PostgreSQL 集成测试；Memory/Approval 持久化在阶段 7，Runs/Events 在阶段 5，Orchestration 在阶段 6，避免与后续执行链迁移重复。

域级进入条件：

- domain 实体和值对象不依赖 ORM、Pydantic 或旧 `athena`。
- domain repository port 已定义并有旧类型转换测试。
- 新 ORM model 与 repository 通过构造函数注入数据库会话，不依赖旧 `Database` 门面。

先搬最稳定、最容易隔离的数据库代码：

- `infrastructure/postgre/models.py` 拆到 `infrastructure/persistence/postgres/models/`。
- `model_converters.py` 移到各 repository 旁边。
- `session_repository.py`、`message_repository.py`、`file_repository.py`、`knowledge_base_repository.py`、`tool_repository.py` 逐个实现新的 domain port。
- 增加 Unit of Work，逐步替代 `Database` 直接暴露全部 repository 的门面。

顺序：sessions/messages -> files/knowledge -> tools/MCP 配置。Runs/Events、Memory/Approval 和 orchestration 分别归入阶段 5、7、6 的完整业务切片。

Session/Message 当前切片的验收：

- [x] 新 PostgreSQL ORM model 只位于 `infrastructure/persistence/postgres/models/`。
- [x] 新 repository 只返回 `domain.sessions` 类型。
- [x] application/domain 不导入 SQLAlchemy model。
- [x] 转换函数覆盖 Session、Message、ToolCall 和 AttachmentRef。
- [x] repository 有不依赖生产数据库的单元测试，并补充可运行的 PostgreSQL 集成测试入口。

集成测试运行方式：设置 `ATHENA_TEST_DATABASE_URL` 后执行
`PYTHONPATH=athena_restructured/src .venv/bin/python -m pytest -m integration athena_restructured/tests/integration`；未配置时测试会明确跳过，不能将跳过结果视为数据库环境已验收。

阶段 2 范围验收：新 repository 只返回 domain 类型；application/domain 不导入 SQLAlchemy model；每个 repository 有不依赖生产数据库的单元测试；PostgreSQL 集成测试入口可运行，并已在当前 Docker PostgreSQL 环境中实际通过。阶段 2 无剩余迁移项。

阶段 2 当前实现清单：

- [x] Session/Message ORM、转换器、Repository。
- [x] Attachment/FileChunk ORM、转换器、Repository。
- [x] KnowledgeBase ORM、转换器、Repository。
- [x] FileArtifact/AdapterRegistry ORM、转换器、Repository。
- [x] Tool/MCP ORM、转换器、Repository。
- [x] 注入式 `PostgresUnitOfWork`，不依赖旧 `Database` 门面。
- [x] repository 注入测试和转换器单元测试。
- [x] PostgreSQL 集成测试入口；无 `ATHENA_TEST_DATABASE_URL` 时明确跳过。
- [x] Docker PostgreSQL 环境验收：Session CRUD、Tool/MCP CRUD 和软删除集成测试通过。

## 阶段 3：低风险查询用例和 Controller

状态：已完成。Session CRUD、Session 消息/运行查询，以及 Tools、Provider、Settings、Health、Memory、Retrieval trace、Knowledge base 查询均已建立目标层 Service/Port/DTO/Controller，并通过独立 HTTP 契约测试。

先迁移不改变执行流程的读写接口：

1. Session 创建、列表、详情、重命名。
2. Session 消息列表和运行列表。
3. Tool、Provider、Settings、Health 查询。
4. Memory 查询和 Retrieval trace 查询。
5. Knowledge base 查询。

每个 Controller 改成 DTO -> Service -> Port；用例响应保持当前 HTTP 状态码和 JSON 字段。

验收：API contract 测试通过；Controller 中不再出现 `db.xxx`、`repository`、ORM model 或 storage 调用。

Session CRUD 当前切片：

- [x] `SessionService` 负责创建、列表、详情、重命名和删除用例。
- [x] Session Controller 通过 DTO -> Service -> `SessionRepository`，不直接访问数据库或 ORM。
- [x] `POST/GET /api/sessions`、`GET/PATCH/DELETE /api/sessions/{id}` HTTP 契约测试。
- [x] 迁移应用工厂支持注入 Session、消息/运行和各查询 Service；正式 PostgreSQL 依赖组装列为阶段 8 入口切换工作。

阶段 3 查询切片验收：

- [x] Session 消息列表通过 `SessionMessageQueryService -> MessageRepository` 查询。
- [x] Session 运行列表通过 `RunQueryService -> RunQueryPort` 查询。
- [x] Tool、Provider、Settings、Health 查询通过独立 Service/Port 或 bootstrap 健康用例提供。
- [x] Memory、Retrieval trace、Knowledge base 查询通过 application Service 和注入 port/repository 提供。
- [x] 查询 Controller 只处理 DTO、领域对象转换和 HTTP 异常映射，不导入 ORM、具体 repository、`db` 或 storage。
- [x] 阶段 3 HTTP 契约测试覆盖成功响应、DTO 字段和资源不存在错误。

## 阶段 4：Session、附件和知识文档写入

状态：已完成。会话附件和知识库文档写入、解析分块、向量/图索引适配、失败回滚和后台 Worker 均已建立目标层边界并通过契约/端到端测试。

这是第一个完整业务闭环，依赖阶段 2 和 3：

- `AttachmentService` 负责文件名校验、存储、附件记录、清理和任务入队。
- `IngestionService` 负责解析、分块、向量索引、图索引和状态转换。
- `KnowledgeDocumentWorker` 只负责取任务并调用 `IngestionService`。
- `FileIntelligenceRuntime` 拆为 application service 加多个 infrastructure port。

迁移来源：`gateway/routes/files.py`、`gateway/routes/sessions.py` 中的 multipart 逻辑、`core/files/runtime.py`、`core/files/ingestion.py`、`core/files/analysis.py`、文件 adapters。

验收：上传、删除、解析失败回滚、文件访问权限和知识检索的迁移区契约/端到端测试全部通过；旧文件智能测试中依赖共享数据库固定 ID 的用例需要独立测试数据库，避免跨运行残留主键冲突。

阶段 4 当前切片验收：

- [x] `AttachmentService` 负责会话校验、文件名校验、适配器选择、流式存储、附件持久化和任务入队。
- [x] 会话附件 Controller 提供列表、上传、支持类型、详情和删除接口。
- [x] 上传失败补偿包含任务取消、附件软删除和 blob 丢弃。
- [x] 新增 Service/Controller HTTP 契约测试和编译检查。
- [x] `KnowledgeBaseService` 和知识库文档上传/版本查询/删除。
- [x] `IngestionService` port 及解析、分块、向量和图索引适配器。
- [x] `KnowledgeDocumentWorker` 通过 application service 处理任务并实现失败重试。
- [x] 阶段 4 端到端上传、解析失败回滚、访问权限和知识检索验收；阶段 4 专项测试和 Docker PostgreSQL 集成测试通过。

## 阶段 5：命令、运行和事件

状态：已完成目标层切片。命令、运行、事件/SSE、执行协调和命令消费者均已建立目标层实现、旧适配器和契约测试；旧入口保留到阶段 8 统一切换。

先把 HTTP 提交与后台执行分离，再迁移复杂 Agent 流程：

- `RunCommandService`：创建命令、幂等、会话忙状态、取消命令。
- `RunQueryService`：运行列表、详情、事件读取。
- `RunExecutionService`：一次 Root run 的状态转换和结果持久化。
- `CommandConsumer`：只负责消费命令、调用 Service、处理重试和生命周期。
- `RuntimeEventPublisher`、SSE 适配器放在接口/基础设施边界。

迁移来源：`gateway/routes/sessions.py`、`gateway/routes/commands.py`、`gateway/routes/events.py`、`runtime/command_consumer.py`、`runtime/transport.py`、`infrastructure/postgre/repositories/agent_store.py`。

阶段 5 当前切片验收：

- [x] Runs 领域命令、命令状态和运行摘要使用无框架依赖的目标类型。
- [x] `RunCommandService` 负责会话存在性、运行/消息 ID、幂等入队、会话忙错误和取消命令。
- [x] `RunQueryService` 通过运行 port 提供会话运行列表和运行详情。
- [x] 旧 `AgentStore` 通过适配器接入新的命令和运行 port，不向 application 暴露旧 Pydantic/ORM 类型。
- [x] 新 HTTP Controller 保持提交、取消和命令状态查询的路径、状态码和 JSON 字段。
- [x] fake-port 测试覆盖幂等、会话忙、取消、资源不存在和命令状态查询。
- [x] Application event domain 类型和事件持久化/发布 port。
- [x] SSE 历史重放、`Last-Event-ID`、实时订阅、心跳和断开清理。
- [x] `RunExecutionService` 负责运行状态转换、结果持久化和终态事件。
- [x] `CommandConsumer` 只负责领取命令、调用执行 Service、失败收敛和停止生命周期。

验收：命令幂等、取消、恢复、事件顺序、SSE 和失败状态保持不变；迁移区全量测试和 Docker PostgreSQL 集成测试通过。Approval 仍作为独立后续契约切片，不阻塞本阶段命令/运行/事件边界。

## 阶段 6：Root/Worker 编排和 LangGraph

状态：已完成目标层切片。阶段 6 的 Plan/Task/DAG、OrchestrationService、Worker 调度/执行、失败重试、审批等待恢复、结果汇总和 LangGraph infrastructure adapter 均已建立；旧入口保留到阶段 8 统一切换。

这是风险最高的迁移，必须在阶段 5 稳定后进行：

- Plan/Task/DAG 验证留在 `domain/orchestration`。
- 计划创建、物化、调度、结果汇总放入 `application/orchestration/OrchestrationService`。
- `TaskExecutor` 和 Worker agent 变成 application worker。
- LangGraph 节点、checkpointer 和 graph builder 放入 `infrastructure/orchestration/langgraph`。
- `runtime/langgraph_runtime.py` 拆成 application facade 和 infrastructure adapter。

迁移来源：`agents/`、`planning/`、`runtime/nodes/`、`runtime/execution_loop/`、`runtime/task_executor.py`、`runtime/langgraph_runtime.py`。

验收：Root/Worker、审批暂停恢复、DAG 并行、失败重试、结果汇总和现有 orchestration 边界测试通过；目标迁移区全量测试和 Docker PostgreSQL 集成测试通过。

阶段 6 当前切片验收：

- [x] Plan/Task/PlanEdge/状态使用无框架依赖的目标类型。
- [x] Plan 校验覆盖节点与任务一致性、重复边、自依赖、环、工具和 Worker 类型边界。
- [x] DAG 提供确定性的依赖查询、就绪任务和依赖失败传播。
- [x] `OrchestrationService` 通过 port 校验并提交计划，不直接依赖 ORM、LangGraph 或旧 Runtime。
- [x] 旧 `PlanSubmission`/`TaskDefinition` 通过 infrastructure 转换器接入目标领域类型。
- [x] application `TaskExecutor`/Worker 调度通过 Worker port 支持并行 DAG、失败重试和审批等待恢复。
- [x] Result synthesizer port 汇总终态 Worker 结果。
- [x] infrastructure 旧编排仓储、Worker registry 和 LangGraph graph adapter 已建立。

## 阶段 7：Memory、Tools、MCP 和外部集成

状态：已完成目标层切片。阶段 7 的 Memory、Approval、Tools、MCP 和外部集成边界均已建立；旧入口和旧实现继续保留到阶段 8 统一切换。

阶段 7 的搬迁顺序：

1. Memory：领域实体/端口、查询和写入应用服务、旧记忆服务适配器，再迁移后台写入 worker、压缩和事实抽取。
2. Approval：审批领域实体/仓储端口、审批 Service，再接入现有工具审批和运行恢复边界。
3. Tools：工具目录和配置领域模型、Service、执行端口，再迁移内置工具和审批策略。
4. MCP 与外部集成：MCP Service/端口，最后迁移 LLM、sandbox、pgvector、Neo4j、文件解析器的具体实现。

阶段 7 当前切片验收：

- [x] Memory 领域实体覆盖记录、检索、分页和写入命令，且不依赖旧 `athena`、Pydantic 或基础设施。
- [x] `MemoryService` 通过 `MemoryPort` 提供初始化、检索、写入、修订、有效性和删除用例。
- [x] `LegacyMemoryAdapter` 将旧 `LongTermMemoryService` 的字典接口转换为目标领域类型。
- [x] Memory 领域/application 边界测试和适配器单元测试通过。
- [x] 目标 Memory HTTP Controller 和 bootstrap 工厂支持注入 `MemoryService`，并保留旧查询 Service 兼容路径。
- [x] Memory 后台写入 worker、事实提取/解析工作流通过 `MemoryJobPort`、`MemoryFactExtractorPort` 和 `MemoryResolverPort` 接入目标 application。
- [x] 旧 Memory v2 工作流、JobRepository 和 worker 已建立 infrastructure adapter。
- [x] 旧 Memory 触发器、事实提取器和候选解析器均已建立显式 infrastructure adapter。
- [x] Approval 领域状态、请求实体、仓储/事件端口和 `ApprovalService` 已建立。
- [x] 旧 `ApprovalStorePort` 和事件发布能力已建立目标层 adapter。
- [x] Tool 执行请求/结果、治理服务和 `ToolExecutionPort` 已建立，旧 `UnifiedToolManager` 已隔离到 infrastructure。
- [x] `MCPService` 和 `MCPPort` 已建立，旧 `MCPManager` 已隔离到 infrastructure。
- [x] 目标 Approval、Tool governance 和 MCP Controller 已接入迁移期 `bootstrap.create_app`。
- [x] Tool/MCP PostgreSQL repository 真实数据库 CRUD 集成测试已通过。
- [x] LLM、sandbox、pgvector、Neo4j、文件解析器和文件存储均通过 infrastructure integration adapter 暴露，具体 SDK 未进入 domain/application。
- [x] Memory worker、Approval、Tools/MCP 和集成端口专项测试通过。

阶段 7 验证：目标迁移区全量测试和 Docker PostgreSQL 集成测试通过（`81 passed, 2 skipped`，覆盖率 `90.35%`，硬门槛 `--cov-fail-under=90`）；编译检查、`git diff --check` 和整个 `athena_restructured/src/athena_restructured` 的目录外 `athena` 导入扫描均通过。

- 阶段 7 已完成目标层的 Memory、Approval、Tools、MCP 和外部集成端口；旧 `core/*` 实现仍由旧入口使用，目标包不再静态依赖其实现。阶段 8 仍负责入口切换和旧目录退役。

验收：memory、tool、approval、MCP、sandbox 和 provider 测试通过；目标 `domain/application` 不出现具体 SDK import，具体 SDK 只由 `infrastructure` 适配器和迁移期旧实现使用。

## 阶段 8：迁移期入口切换和兼容边界

状态：已完成。目标层入口、完整兼容路由、生产 PostgreSQL 依赖图、Worker 生命周期、外部集成原生实现、旧实现等价对照和旧目录退役均已通过验收。

- [x] 新建 `bootstrap/app.py`、`bootstrap/lifespan.py`、`bootstrap/dependencies.py`。
- [x] 默认入口注册全部兼容 HTTP 路由；旧路由路径静态对照无缺失。
- [x] 默认入口组装可测试的目标层进程内依赖图，覆盖 Session、Run、Event、Approval、Memory、Files、Knowledge、Tools、MCP 和查询服务。
- [x] 默认生产依赖图按 `ATHENA_DATABASE_BACKEND=postgres` 组装目标 PostgreSQL engine、Session/Message、Run/Command/Event、Approval、Memory、Retrieval、Files/Knowledge、Tools/MCP repository；`ATHENA_WORKERS_ENABLED=true` 时将命令消费者和知识文档 Worker 纳入 lifespan。
- [x] 目标 Plan/Task orchestration repository 已接入 PostgreSQL 依赖图，覆盖 DAG 原子领取、依赖失败传播、任务完成状态收敛、审批等待/恢复、重试和 TaskExecution 恢复。
- [x] 默认生产依赖图已替换外部集成兼容实现：目标 LLM/Embedding provider、pgvector 文件索引、Neo4j resource、Docker sandbox、原生工具执行和 PDF/Word/Excel/CSV/代码/文本解析器均由目标包组装；未启用服务仍以目标层生命周期和明确错误返回。
- [x] 旧入口和旧实现通过目标入口完成等价运行验证；两入口路由方法/路径均为 53 条且 `/health` 均返回 200。
- [x] 删除无调用者的旧目录；目标源码中的无调用者 `legacy_*`/`compat` 适配器已删除。

迁移区全量测试、入口对照和删除闸门均已通过；旧包已退役。完整证据见 `MIGRATION_INVENTORY.md`。

## 每个迁移单元的固定步骤

1. 画出当前模块的输入、输出和依赖。
2. 先在目标目录定义 port、domain 类型和 application service。
3. 编写 service 单元测试，再接入旧 adapter。
4. 把一个 Controller 或 Worker 切换到新 Service。
5. 运行对应测试和架构测试。
6. 保留旧实现一段时间，确认无调用者后再删除。

## 不应提前做的事情

- 不在第一阶段移动 LangGraph、Agent graph 或所有 `core` 文件。
- 不把旧 `Database` 门面原样复制到新目录。
- 不同时更改 API 字段、数据库 schema 和业务行为。
- 不通过批量替换 import 来假设模块边界已经成立。

## 阶段 9-14：完整迁移和旧包退役

剩余代码的逐目录台账、目标位置和验收条件见 [`MIGRATION_INVENTORY.md`](MIGRATION_INVENTORY.md)。阶段 9-14 必须按依赖顺序完成，不能只保留目标层 port 或 legacy adapter 就标记完成。

阶段 9：生产持久化切换。为 Run、Command、Event、Approval、Memory、Retrieval、Plan/Task、ToolCall 和 Knowledge Job 建立目标原生 repository；使用 `Settings.postgres_url` 创建 engine、session factory 和 Unit of Work；运行 schema、软删除、幂等、事件序号和并发领取的 PostgreSQL 集成测试。

阶段 10：外部集成切换。把 LLM、Embedding、文件解析器、pgvector、Neo4j、Sandbox、MCP 和本地文件存储从兼容实现替换为目标 infrastructure 实现；每个集成提供启动、关闭、超时、重试和故障降级测试。

阶段 11：运行时和 Worker 切换。迁移 context providers、harness、compression、task understanding、execution loop、Root/Worker LangGraph、command consumer、knowledge worker 和 memory worker；在 `bootstrap/lifespan.py` 统一生命周期，并验证取消、审批暂停/恢复、重试和失败收敛。

阶段 12：调用者切换和删除适配器。生产入口、前端契约测试和端到端测试已指向目标 repository/integration/worker，14 个 `legacy_*`/`compat` 文件已删除；目标源码和测试扫描没有旧 `athena` 导入。

阶段 13：旧目录退役。在全量 API、SSE、数据库、Worker 和启动关闭对照测试通过后删除旧 `athena/` 目录；保留 Git 历史，不保留运行时兼容导入。

阶段 14：最终验收。执行本台账的删除闸门、覆盖率不低于 95%、目标应用默认入口启动探针、完整路由清单、编译检查和生产配置启动/关闭测试，并把本台账所有条目标为“已迁移”。

### 剩余代码的执行批次

| 批次 | 迁移范围 | 目标交付物 | 通过条件 | 回滚点 |
| --- | --- | --- | --- | --- |
| A | `config`、`utils`、`observability`、`context`、`runtime/context`、`task_understanding` | `bootstrap/config`、`shared`、`application/runs/context` 和 observability port | 目标入口只读取目标配置；上下文/任务理解单测和旧行为对照通过 | 保留旧入口，目标 provider 可切回 in-memory |
| B | `core/files`、`core/retrieval`、`core/memory` | 完整 parser、chunk/retrieval、memory write job 和 worker | 多格式文件、权限、失败补偿、检索 trace、记忆写入对照通过 | 保留目标文件任务队列，暂停新索引 worker |
| C | `core/llm`、`core/tools`、`core/graph`、`core/sandbox`、`infrastructure/*` 外部集成 | LLM/Embedding、工具执行、pgvector、Neo4j、Docker Sandbox 原生 adapter | 启停、超时、重试、降级、资源释放和真实服务集成测试通过 | 通过 feature flag 回到明确失败的目标端口 |
| D | `agents`、`planning`、`runtime/execution_loop`、`runtime/nodes`、`runtime/services`、`runtime/orchestration` | Root/Worker/LangGraph application facade 和 infrastructure graph | 取消、审批暂停/恢复、并发 DAG、重试、事件顺序和最终结果对照通过 | 命令消费者保持停止，HTTP 提交仍可查询状态 |
| E | `infrastructure/postgre`、`workers`、`main.py`、`container.py`、前端/旧测试 | 全部目标 repository、worker 生命周期和唯一生产入口 | 真实 PostgreSQL 全表验收、前端契约、启动/关闭和全量测试通过 | 旧入口保留为只读诊断，不参与生产组装 |
| F | 删除 `legacy_*`/`compat` 和旧 `athena/` | 删除记录、依赖扫描和发布包清单 | 删除闸门全部通过，目标源码/测试零旧包导入 | Git 保留历史；若闸门失败，仅回滚删除提交 |

每个批次都必须先提交目标 port/domain，再提交 infrastructure 实现和调用者切换，最后才能删除旧文件。批次之间不允许把“有 adapter”当作“已迁移”；只有行为对照、调用者切换和删除闸门同时通过，台账状态才改为“已迁移”。
