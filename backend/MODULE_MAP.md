# 当前模块迁移映射

下面的映射按目标职责组织。迁移时以“先提取接口，再迁移实现，再切换调用者”为准，不直接把旧目录整体复制到目标目录。

| 当前模块 | 目标位置 | 迁移说明 |
| --- | --- | --- |
| `gateway/routes/*.py` | `interfaces/http/controllers/` | 保留路由声明和 HTTP 映射；业务逻辑提取到 application service |
| `gateway/routes/schemas.py` | `interfaces/http/dto/` | 请求、响应 DTO；不作为 domain entity 使用 |
| `gateway/auth/*` | `interfaces/http/middleware/` | 认证和会话中间件 |
| `gateway/approval.py` | `application/approval/` | 审批用例；HTTP 适配留在 Controller |
| `models/session.py`, `models/message.py` | `domain/sessions/` | 先提取无框架依赖的会话、消息和值对象；旧 Pydantic 模型通过 adapter 转换 |
| `models/file.py` | `domain/files/` | 附件、分块、文件状态和值对象 |
| `models/tool.py`, `models/mcp.py` | `domain/tools/` | 工具配置、风险等级和 MCP 配置 |
| `models/approval.py` | `domain/approval/` | 审批实体和决定规则 |
| `models/json_models.py` | 按业务域拆分 | 只有业务类型进入 domain；传输专用类型进入 DTO |
| `contracts/ports.py` | 各 `domain/*/ports.py` | 按能力拆分，不保留一个全局 port 文件 |
| `contracts/events.py` | `domain/*/events.py` | 领域事件和应用事件分开 |
| `contracts/commands.py` | `domain/runs/commands.py` | 命令类型和命令状态规则 |
| `core/files/*` | `application/files/` + `domain/files/` | 运行时编排进 application，解析器/存储/向量实现进 infrastructure |
| `core/memory/*` | `application/memory/` + `domain/memory/` | 记忆规则进 domain，用例和工作流进 application |
| `core/tools/*` | `application/tools/` + `infrastructure/integrations/` | 工具策略进 domain，执行器和 MCP 进 infrastructure |
| `core/llm/*` | port 在 domain/application，实现在 `infrastructure/integrations/llm/` | application 不依赖具体 SDK |
| `core/sandbox/*` | port 在 domain，Docker 实现在 `infrastructure/integrations/sandbox/` | Workspace policy 和执行实现分开 |
| `planning/*` | `domain/orchestration/` | Plan、DAG、校验规则 |
| `agents/*` | `application/runs/` 或 `application/orchestration/` | Agent 用例编排；LangGraph 适配单独放 infrastructure |
| `runtime/command_consumer.py` | `workers/command_consumer.py` | 只保留消费循环和调用 service |
| `runtime/task_executor.py` | `workers/orchestration_worker.py` | Worker 入口；调度规则由 application service 提供 |
| `runtime/nodes/*` | `infrastructure/orchestration/langgraph/nodes/` | LangGraph 技术节点 |
| `runtime/execution_loop/*` | `application/runs/` + `infrastructure/orchestration/langgraph/` | 业务执行用例与图适配拆开 |
| `runtime/langgraph_runtime.py` | application facade + LangGraph adapter | 不整体搬迁 |
| `runtime/context/*` | `application/runs/context/` | 上下文规划和获取用例 |
| `infrastructure/postgre/models.py` | `infrastructure/persistence/postgres/models/` | ORM 只留在基础设施 |
| `infrastructure/postgre/repositories/*` | `infrastructure/persistence/postgres/repositories/` | 实现各 domain port |
| `infrastructure/postgre/database.py` | `infrastructure/persistence/postgres/unit_of_work/` | 生命周期和事务组装，不作为业务服务定位器 |
| `infrastructure/pgvector/*` | `infrastructure/integrations/pgvector/` | 向量库 adapter |
| `infrastructure/neo4j/*` | `infrastructure/integrations/neo4j/` | 图数据库 adapter |
| `infrastructure/embedding/*` | `infrastructure/integrations/llm/embedding/` | embedding adapter |
| `infrastructure/sandbox/*` | `infrastructure/integrations/sandbox/` | Docker adapter |
| `main.py`, `container.py` | `bootstrap/` | 应用工厂、生命周期和依赖图 |
| `utils/*` | `shared/` 或对应业务域 | 只有真正跨域的无业务工具进入 shared |

## 迁移单元

每次迁移以一个可验证的业务用例为单位：

```text
domain contract -> application service -> infrastructure adapter -> controller/worker switch -> old code removal
```

一个用例完成后，目标目录应能独立测试；不要先迁移一个大目录再寻找它的职责。
