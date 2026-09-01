# Athena 系统架构

## 1. 架构定位

Athena 采用模块化单体架构。模块之间遵循单向依赖关系：界面调用网关，网关调用核心能力，核心能力通过端口依赖基础设施适配器；领域模型作为各层共享的数据契约。

```text
desktop -> gateway -> core ports <- infrastructure adapters
                         ^
                         |
                     domain models
```

这种分层使业务规则不依赖具体数据库、向量库或网络传输实现，也便于在测试中注入替代实现。

## 2. 工具边界

所有暴露给 Agent 的工具声明都使用 `athena/core/tools` 中的 `ToolSpec`。文件能力和 Agent 能力通过 Provider 适配到工具层，但业务逻辑仍归各自的核心服务所有。

`ToolRegistry` 负责安装工具声明，`ToolCatalogService` 是持久化工具治理配置同步的唯一责任者。可信的会话、运行和工具调用标识由 `ToolContext` 提供，不作为模型可任意修改的普通参数。

## 3. 存储边界

`athena/core/memory` 负责记忆生命周期、检索编排和双写补偿策略，并通过 `MemoryRepository` 与 `MemoryVectorStore` 端口访问存储。

具体实现位于基础设施层：

```text
athena/infrastructure/
├── sqlite/       # SQLAlchemy、SQLite 和 FTS5 仓库
└── chroma/       # ChromaDB 向量适配器
```

数据库实现统一从 `athena.infrastructure.sqlite` 引入。核心服务通过组合根显式接收存储依赖，不维护隐藏的模块级数据库定位器。

## 4. Runtime 组合

`athena/main.py` 是应用组合根，负责创建 `RuntimeContainer`，初始化数据库、LLM、工具、审批、记忆、文件运行时和 LangGraph，并将容器挂载到 `FastAPI.app.state`。

`agent_runtime` 包含命令消费者、LangGraph 图、取消注册表、流式事件传输和启动恢复协调器。Gateway 只负责校验请求并写入版本化 `Command`；Runtime 负责领取命令、执行图和发布 `ApplicationEvent`。

## 5. 事件与流式输出

Durable Event 写入 SQLite，使用会话级 `event_id` 支持 SSE 的 `Last-Event-ID` 重放；Realtime Event 通过进程内 `RealtimeTransport` 降低展示延迟，不承担历史恢复职责。消息正文同时保存为带版本的 `StreamSnapshot`，用于客户端重连后的状态恢复。

SSE 端点必须先登记实时订阅，再读取持久化事件。实时通知只作为“有新事件”的提示，实际游标始终从 SQLite 读取，因而不会因为通知丢失而跳过 Durable Event。

## 6. 维护约束

- 新增 Agent 能力时，先定义核心接口，再在 Gateway 或基础设施层增加适配器。
- 修改命令或事件字段时必须更新对应的 Pydantic 模型、存储映射、客户端类型和测试。
- 任何需要跨请求恢复的状态都必须进入 SQLite 或 LangGraph 检查点，不能只保存在进程内字典。
- 变更运行状态转移、租约、幂等和 SSE 游标逻辑时，应补充并发、重连和重复投递测试。
