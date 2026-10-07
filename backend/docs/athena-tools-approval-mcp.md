# 工具、审批与 MCP 实现

依赖：[系统实现总说明](athena-system-overview.md)、[运行时与数据底座](athena-runtime.md)。被 [HTTP API 与事件协议](athena-api.md)、[前端复刻](athena-frontend.md) 依赖。

## 1. 统一工具协议

每个工具转换为 `ToolSchema(name, description, parameters, require_approval, risk_level, execution_mode)`。`execution_mode` 为 `native` 或 `mcp`；`risk_level` 为 `low/medium/high`。`UnifiedToolManager` 保存注册工具、disabled 集合和受信 ToolRuntime，向 LangChain 暴露 StructuredTool。

工具调用必须传入 session_id、run_id、tool_call_id；Root 调用还可带 plan_id/task_id/worker_run_id/agent_role/depth。调用过程创建 tool_calls 记录和 `tool.started/tool.completed` 事件，结果统一为 `ToolResult(status, output, error, duration_ms)`，status 为 success/failed/denied/timeout。

调用前检查：工具已注册、enabled、允许的 agent role/tool list、递归 depth 和审批决定。disabled 工具不得被 LLM schema 暴露；未批准的调用不得执行 handler。

## 2. 当前实际内置工具

启动时注册 8 个基础工具，文件智能运行时再注册 12 个文件工具：

| 工具 | 参数 | 默认治理 | 行为 |
| --- | --- | --- | --- |
| `read_local_file` | path, encoding=utf-8 | low，无审批 | 经过 PathSecurityFilter 读取文本 |
| `write_file` | path, content, encoding=utf-8, append=false | medium，审批 | 覆盖或追加写入；自动创建父目录 |
| `list_directory` | path=".", include_hidden=false | low，无审批 | 列出文件/目录和文件大小 |
| `exec_shell` | command, timeout=60, cwd=null, check=true | high，审批 | Docker `/bin/sh -lc`，网络/资源/输出受配置限制 |
| `get_local_file_info` | path | low，无审批 | size_kb、line_count、is_binary、file_type |
| `read_local_file_section` | path, start_line>=1, limit<=200 | low，无审批 | 返回带行号局部文本 |
| `search_local_file` | path, pattern | low，无审批 | 正则匹配行号和内容 JSON |
| `read_local_file_full` | path, encoding | low，无审批 | 仅允许小于 50 KB 的文本 |

路径工具使用 allowed_dirs/blocked_dirs/allowed_extensions 的 PathSecurityFilter；路径遍历、写保护路径、二进制读取和非法正则转为工具错误。

文件智能工具：

| 工具 | 行为 |
| --- | --- |
| `list_files` | 当前会话附件 + 全局就绪知识库文档 |
| `get_file_info(file_id)` | 公开元数据、状态、能力和解析统计 |
| `read_file(file_id, locator?, limit=10)` | 按 page/sheet/path 读取已解析 chunk，limit 1..50 |
| `search_file(file_id, query, limit=10)` | FTS + vector + RRF + rerank |
| `extract_table(file_id)` | 读取解析时缓存的表格 |
| `summarize_file(file_id, summary_type="general")` | 多级 LLM 摘要并缓存 artifact |
| `analyze_file(file_id, task)` | 适配器分析；图片在视觉模型可用时做 vision |
| `analyze_codebase(file_id)` | 语言、文件数、符号和依赖概览 |
| `find_symbol(file_id, name)` | 模糊查找代码符号 |
| `find_definition(file_id, name)` | 查找符号定义 |
| `find_references(file_id, name)` | 查找 incoming 引用 |
| `get_call_graph(file_id, symbol, direction="both")` | incoming/outgoing/both 依赖关系 |

## 3. 审批闸门

LLM 一次返回多个工具调用时，先识别所有 `require_approval=true` 的调用并创建同一 `approval_batch_id`。每项持久化 tool_call_id、tool_name、arguments、risk_level、session/run/plan/task/worker thread。发布 `approval.required` 后使用 LangGraph `interrupt` 暂停。

恢复值必须包含相同 batch_id 及每一项决定，且值只能 `approved/denied/cancelled`。缺项、未知项或 batch_id 不一致都拒绝恢复。allow/deny API 只是生成 `approval.resolve` 命令；CommandConsumer 才负责恢复图。审批拒绝写入 denied tool result，允许才执行 handler，cancelled 结束批次不执行。

审批日志保存 created_at/decided_at，统计当天总数和批准率。应用重启保留 pending approvals，用户可继续处理。

## 4. 内置 shell 沙箱

`exec_shell` 要求 `SANDBOX_ENABLED=true` 且 `EXEC_SHELL_ENABLED=true`。每个 session/run 建立隔离 workspace，cwd 必须解析到 workspace 内；命令通过 Docker runner 执行，默认镜像 `python:3.12-slim`、网络 `none`，超时不超过 `SANDBOX_MAX_TIMEOUT`，输出不超过 `SANDBOX_MAX_OUTPUT_BYTES`，还受 memory/cpu/pids 限制。Docker 不可用时返回 `sandbox_unavailable`；运行结束关闭 run 容器。

## 5. MCP

MCP 配置持久化到 `mcp_servers.config_json`，启动时恢复 enabled 服务。command 可以是字符串或 argv 列表，args/env/image_id/network_policy/enabled 一并保存；env 只用于子进程，API 列表掩码。

MCPManager 为每台服务创建 MCPClient，发现远程工具后通过 MCPToolAdapter 转成 `MCPTool` 注册到 UnifiedToolManager，并在 `tools` 表 reconcile。远程工具沿用 native 工具的风险/审批/启用治理。注册逐台隔离；失败项保存 failed 状态和 error，不影响其他服务。注销顺序为移除工具、关闭 session/subprocess、软删除服务器及其工具配置。

## 6. 治理持久化

`ToolCatalogService.reconcile()` 只更新注册描述、参数、execution mode 和 MCP 来源；用户修改的 risk_level、require_approval、enabled 必须保留。PATCH 同时更新内存 ToolSchema、disabled 集合和 `tools` 表。重启后先注册工具，再加载治理配置，保证 LLM schema 和执行策略一致。
