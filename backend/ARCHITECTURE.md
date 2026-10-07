# 目标架构

## 分层职责

### Interfaces

Controller 只做 HTTP 输入解析、认证上下文读取、DTO 转换、调用 Application Service 和异常到 HTTP 响应的映射。Controller 不直接访问 repository、ORM、storage 或外部 SDK。

### Application

Service 表示一个可测试的业务用例，例如创建会话、提交运行、上传附件、处理审批。Service 负责校验用例前置条件、协调多个 port、控制事务和发布应用事件，但不依赖 FastAPI 或具体数据库实现。

### Domain

Domain 保存实体、值对象、状态机、领域规则、领域事件和 port。领域对象不能通过 `Database` 门面访问数据，也不能导入基础设施实现。

### Infrastructure

Infrastructure 实现 domain/application 定义的 port，包括 PostgreSQL repository、ORM model、Neo4j、pgvector、LLM、文件存储、Docker、MCP 和 LangGraph adapter。这里可以依赖第三方 SDK。

### Bootstrap and Workers

Bootstrap 负责构建对象图和生命周期。Workers 负责后台循环，但业务动作必须调用 Application Service，不能把业务规则写进消费循环。

## 目标结构

```text
athena_restructured/
├── src/athena_restructured/
│   ├── interfaces/http/
│   │   ├── controllers/
│   │   ├── dto/
│   │   ├── middleware/
│   │   └── sse/
│   ├── application/
│   │   ├── sessions/
│   │   ├── runs/
│   │   ├── files/
│   │   ├── memory/
│   │   ├── knowledge/
│   │   ├── tools/
│   │   └── approval/
│   ├── domain/
│   │   ├── common/
│   │   ├── sessions/
│   │   ├── runs/
│   │   ├── files/
│   │   ├── memory/
│   │   ├── knowledge/
│   │   ├── tools/
│   │   ├── approval/
│   │   └── orchestration/
│   ├── infrastructure/
│   │   ├── persistence/postgres/
│   │   │   ├── models/
│   │   │   ├── repositories/
│   │   │   └── unit_of_work/
│   │   ├── integrations/
│   │   │   ├── llm/
│   │   │   ├── neo4j/
│   │   │   ├── pgvector/
│   │   │   ├── storage/
│   │   │   ├── sandbox/
│   │   │   ├── mcp/
│   │   │   └── file_parsers/
│   │   └── orchestration/langgraph/
│   ├── workers/
│   ├── bootstrap/
│   └── shared/
└── tests/
    ├── architecture/
    ├── unit/
    └── integration/
```

## 硬性边界

1. `interfaces` 不得导入 `infrastructure.persistence` 或 ORM model。
2. `application` 不得导入 FastAPI、SQLAlchemy、Starlette 或具体 repository 类。
3. `domain` 不得导入 `interfaces`、`application`、`infrastructure`、第三方 Web/DB/LLM SDK。
4. `infrastructure` 可以实现 port，但不能反向修改 domain 规则。
5. 一个业务域的 Service 通过该域 port 协作；跨域流程放在 application 层，不在 Controller 中拼接。
6. DTO、domain entity、ORM model 三者分开，转换集中在边界适配器中。
7. 新代码禁止通过 service locator 或隐式全局变量获取依赖。

## 业务域归属

| 业务域 | Application Service | Domain 内容 | 主要基础设施 |
| --- | --- | --- | --- |
| sessions | `SessionService` | Session、Message、状态规则 | session/message repository |
| runs | `RunCommandService`、`RunExecutionService` | Run、Command、事件 | agent store、checkpoint、event store |
| orchestration | `OrchestrationService` | Plan、Task、DAG 规则 | orchestration repository、LangGraph |
| files | `AttachmentService`、`IngestionService`、`FileRetrievalService` | Attachment、Chunk、访问规则 | file repository、storage、vector store |
| knowledge | `KnowledgeBaseService` | KnowledgeBase、文档状态 | knowledge repository、graph indexer |
| memory | `MemoryService` | Memory、Revision、记忆规则 | memory repository、vector/graph store |
| tools | `ToolService` | Tool、ToolPolicy、RiskLevel | tool repository、MCP、sandbox |
| approval | `ApprovalService` | Approval、Decision、状态规则 | approval store、event publisher |
