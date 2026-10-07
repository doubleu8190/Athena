# Athena 前端复刻说明

依赖：[系统实现总说明](athena-system-overview.md)、[HTTP API 与事件协议](athena-api.md)、[运行时与数据底座](athena-runtime.md)。

## 1. 技术和布局

使用 React 18 + TypeScript + Vite + Zustand。`frontend/src/App.tsx` 提供根布局、active session、视图切换和命令分发；`api/client.ts` 封装 fetch；`hooks/useSessionEventStream.ts` 建立 EventSource；`store/chatStore.ts` 保存 UI 状态。

主界面由 NavRail、Sidebar、Chat、ActivityPanel 组成。管理视图包括 SessionDetail、Approvals、Memory、Tools、Providers、MCP、Knowledge Base、Retrieval、Settings。组件不直接访问数据库，只调用 apiClient。

## 2. API client 行为

`request()` 拼接 baseUrl，非 FormData 自动设置 JSON Content-Type；非 2xx 读取 body 并抛出 `API <status>: <text>`；204 返回 undefined。上传使用 FormData，不手动设置 boundary。生产/开发都使用相对 `/api`，Vite 开发服务器将其代理到 8000。

必须实现的 client 方法：healthCheck、list/create/get/update/delete session、getMessages、submitRun、cancelSession、list/upload/delete attachments、knowledge base CRUD、approval list/respond/batch/logs/stats、tool list/update、memory list/get、provider list/test、settings、MCP list/register/delete、retrieval runs list/detail。

## 3. Zustand 状态

核心状态：

- sessions、activeSessionId、messages、attachments；
- agentStatus：idle/thinking/running/paused/waiting_approval/completed/error；
- thinking：active/content/messageId；
- steps：LLM/tool 步骤，含 status、duration、token、error_detail；
- toolCalls：参数、风险、状态、输出/错误；
- pendingApprovals：approval_id、batch、tool、arguments、risk；
- orchestrationTasks：plan/task/title/status/attempt/worker/error；
- executionTimeline、connectionStatus、error/errorDetail。

切换视图前从 localStorage 恢复最近 view；只有 chat、memory、tools、approvals、providers、session-detail、settings、mcp、retrieval 等有效视图可恢复。

## 4. SSE 投影规则

挂载会话后：

1. 先打开 `/api/sessions/{id}/events`，带 `withCredentials=true`。
2. 连接成功立即拉取 pending approvals 和附件，补齐可能早于 SSE 的 durable 状态。
3. 解析每个 envelope；用 session_seq 去重，用 stream_id/chunk_id 组合 answer。
4. `message.started` 创建 assistant 占位；`message.delta` 更新内容；`message.completed` 关闭流；`stream.snapshot` 在缺 chunk 时恢复内容。
5. thinking 事件切换 thinking 状态；approval.required 加入列表并把 agentStatus 置 waiting_approval；approval.resolved 删除对应项。
6. llm/tool started 创建 running step，completed 更新耗时、结果和错误。
7. task queued/started/retrying/completed/failed 更新编排任务；run.started/resumed/running、run.cancelled、run.failed、run.completed 更新会话和 agent 状态。
8. 单条事件解析异常只记录/忽略，EventSource 保持连接；连接关闭后由浏览器重连并使用 Last-Event-ID。

## 5. 用户流程

### 对话

进入应用先加载 sessions；没有会话时创建默认会话。用户输入文本和附件后调用 submitRun，立即展示 queued/running，实际回答全部来自 SSE。发送中允许 cancel；历史消息从 REST 加载，避免只依赖事件缓存。

### 审批

ApprovalDialog 展示工具名、风险和参数。审批决定按同一 `approval_batch_id` 汇总后调用 `respondApprovalBatch`，收到 `approval.resolved` 后清除卡片并继续显示运行状态。

### 文件/知识库

Chat 上传会话附件并显示 uploaded/processing/ready/failed；KnowledgeBaseView 管理知识库、文档导入、删除和版本。文件 ready 前工具返回 waiting，前端通过 attachment_updated/file_processing_* 刷新。

### 管理页

ToolsView 允许 enabled/risk/approval PATCH；ApprovalsView 显示日志和统计；MemoryView 分页和过期过滤；ProvidersView 做一次性 ping；McpView 注册/删除服务；RetrievalView 查看 run 与 candidate 详情；SettingsView 只读展示。

## 6. 前端复刻验收

验证移动/桌面布局、长文本不溢出、SSE 断线 replay 不重复、delta 乱序最终正确、snapshot 可恢复、审批批次按钮不会重复提交、运行失败后清理 thinking/tool spinner、附件状态和历史刷新一致，以及管理页错误显示 API status/detail。

当前工作树的前端还保留了 `/sessions/{id}/pause`、`/resume` 调用，但后端路由聚合中没有对应 endpoint；复刻时应以实际后端命令契约为准，若要提供暂停功能必须同时增加 `CommandType`、consumer、事件和 route，不能只在前端添加按钮。
