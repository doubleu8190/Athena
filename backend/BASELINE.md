# 阶段 0 基线记录

## 记录范围

本文件记录 2026-10-06 工作树中的旧实现状态，作为迁移期间区分既有问题与迁移回归的参照。基线不代表当前所有功能都已通过；现有未提交修改和失败测试必须保留并单独处理。

## 运行入口

- 旧生产包：`athena/`
- 旧 FastAPI 入口：`athena/main.py`
- 旧 API 前缀：`/api`
- 旧健康检查：`/health`
- 新迁移包：`athena_restructured/src/athena_restructured/`
- 新迁移应用工厂：`athena_restructured.bootstrap.app:create_app`

阶段 0 的新应用工厂只创建 FastAPI 对象和健康路由，不连接 PostgreSQL、Neo4j、LLM、Docker 或消息队列，也不启动后台 worker。

## HTTP 行为清单

当前 `/api` 路由由以下功能域组成：

- 会话、消息、运行和取消：`/api/sessions`
- 有序事件流：`/api/sessions/{session_id}/events`
- 附件和文件智能：`/api/sessions/{session_id}/attachments`
- 命令查询：`/api/commands`
- 审批：`/api/approvals`
- 工具和 Provider：`/api/tools`、`/api/providers`
- 设置和健康：`/api/settings`、`/health`
- 知识库：`/api/knowledge-bases`
- 记忆和检索：`/api/memory`、`/api/retrieval`
- MCP：`/api/mcp`

迁移时每个 Controller 都应保持原有路径、方法、状态码和响应字段，直到对应的 API 对照测试完成。

## 持久化对象清单

当前 SQL schema 包含：

`sessions`、`messages`、`steps`、`tool_calls`、`agent_runs`、`agent_plans`、`agent_tasks`、`agent_commands`、`agent_events`、`stream_snapshots`、`approvals`、`knowledge_document_jobs`。

表和索引的完整定义以 `sql/schema.sql` 为准；阶段 0 不修改 schema。

## 后台任务清单

- `CommandConsumer`：消费命令并驱动 Agent 执行。
- `KnowledgeDocumentWorker`：处理知识文档队列。
- `MemoryWriteJobWorker`：处理记忆写入队列。
- `TaskExecutor`：启动编排计划中的就绪任务。
- LangGraph runtime：负责 Root/Worker 执行、恢复和事件发布。

阶段 0 不移动这些实现；后续迁移必须让 worker 通过 Application Service 执行业务动作。

## 验证快照

阶段 0 的迁移区验证命令：

```bash
.venv/bin/python -m pytest -q \
  athena_restructured/tests/unit \
  athena_restructured/tests/architecture \
  athena_restructured/tests/bootstrap
.venv/bin/python -m athena_restructured.bootstrap.check
```

旧项目全量测试属于独立基线。当前工作树已有若干与迁移无关的失败，不能在阶段 0 中静默归因给新脚手架；每个失败需在后续切片前分类、修复或明确记录。
