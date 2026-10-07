# Athena HTTP API 与事件协议

依赖：[系统实现总说明](athena-system-overview.md)、[运行时与数据底座](athena-runtime.md)。被 [前端复刻](athena-frontend.md) 依赖。

## 1. 通用规则

FastAPI 根路径为 `/api`，默认监听 `http://127.0.0.1:8000`。JSON 请求默认 `Content-Type: application/json`；上传使用 `multipart/form-data`。成功响应使用 2xx；异步提交使用 202。错误体统一为 `{"detail": <stable_code_or_message>}`。认证开启时浏览器必须携带 HttpOnly `athena_session` cookie；API client 使用 `credentials`/同源 cookie。

认证中间件：认证关闭时全部请求放行；认证开启时 `/api/auth/login`、`/api/auth/logout`、`/api/health` 放行，其他接口要求有效 HMAC-SHA256 session cookie。`POST/PUT/PATCH/DELETE` 若带 Origin，只允许服务 host、`http://localhost:5173`、`http://127.0.0.1:5173`。

## 2. 健康和认证

### `GET /api/health`

返回 `{"status":"ok","timestamp":"ISO-8601"}`。它只检查 HTTP 进程，不探测数据库。

### `POST /api/auth/login`

请求：`{"username": string, "password": string}`。凭据错误返回 401 `invalid_credentials`；认证开启且 secret 不足 32 字节返回 503。成功写入 `athena_session`，HttpOnly、SameSite=Strict、按 HTTPS 设置 Secure，默认 24 小时，返回 `{"status":"ok"}`。

### `POST /api/auth/logout`

删除 cookie，返回 `{"status":"ok"}`。

## 3. 会话、消息和运行

### `POST /api/sessions`

请求 `{"title":"New Session"}`。名称默认 `New Session`，返回 Session：`id/title/status/run_id/created_at/updated_at/compression_summary/last_compressed_message_id/last_summarized_message_id`。

### `GET /api/sessions`

返回全部未删除会话，后端按最近更新时间排序。

### `GET /api/sessions/{session_id}`

返回单个 Session；不存在为 404 `session_not_found`。

### `PATCH /api/sessions/{session_id}`

请求 `{"title": string}`。trim 后不能为空，否则 400 `title_empty`；返回更新后的 Session。

### `DELETE /api/sessions/{session_id}`

软删除会话、附件和相关 blob，返回 `{"status":"deleted","session_id": id}`。删除前取消文件任务。

### `GET /api/sessions/{session_id}/messages?limit=N`

返回按时间排序的 Message：`id/session_id/role/content/tool_calls/tool_call_id/run_id/tool_call_record_id/tool_name/message_type/attachments/timestamp`。附件引用只包含 id、filename、mime_type、size_bytes、status。

### `GET /api/sessions/{session_id}/runs`

返回运行摘要数组：`run_id/session_id/status/root_thread_id/error/created_at/updated_at`。

### `POST /api/sessions/{session_id}/runs`（202）

JSON 请求：

```json
{"message":"用户问题", "command_id":"cmd-client-id", "attachment_ids":["file-id"]}
```

也接受 multipart 字段 `message`、`command_id`、多个 `files`；上传文件会先保存为附件再入队。返回：`command_id/run_id/message_id/attachment_ids/status="queued"/deduplicated`。

校验：会话不存在 404；文件名为空或包含 NUL 为 400；不支持类型 415；超过 `FILE_MAX_UPLOAD_BYTES` 为 413；活跃运行或 run/command id 冲突为 409。相同 command_id 和相同 payload 返回 `deduplicated=true`，不同 payload 返回 `command_id_conflict`。

### `POST /api/sessions/{session_id}/cancel`（202）

按会话提交取消命令，适合调用方只有 `session_id` 的场景。可用 query `run_id` 指定目标；不指定时后端查当前 active run。返回 `command_id/run_id/status="queued"`。会话不存在为 404。

### `POST /api/runs/{run_id}/cancel`（202）

按明确的 `run_id` 精确取消单次运行，不会根据会话自动选择其他运行。只有会话 ID 时请使用上面的会话级接口。

不存在为 404 `run_not_found`。

### `GET /api/commands/{command_id}`

返回 `command_id/session_id/run_id/command_type/status/attempt/result/error`。命令不存在为 404 `command_not_found`。result/error 是持久化 JSON。

## 4. SSE 事件

### `GET /api/sessions/{session_id}/events?after=N`

响应 `text/event-stream`，禁止缓存并保持连接。支持 `Last-Event-ID` 覆盖 query after。先重放数据库中 `after < session_seq <= watermark` 的 durable 事件，再监听实时总线；断线重连时重复事件由客户端按 session_seq 丢弃。

每帧格式：

```text
id: 42
event: message.delta
data: {"schema_version":2,"session_seq":42,"event_type":"message.delta",...}
```

data envelope 字段为 `schema_version/session_seq/event_type/durability/session_id/run_id/message_id/attachment_id/stream_id/stream_type/chunk_id/is_complete/parent_run_id/transition_id/payload/occurred_at`。15 秒无事件发送 `: heartbeat`。

客户端必须：按 session_seq 去重；对 `message.delta` 按 stream_id、chunk_id 缓存并只合并连续 chunk；处理 `stream.snapshot` 覆盖当前答案；收到 `message.completed` 将流置为 completed；单条 JSON 解析错误不得关闭 EventSource。

## 5. 附件和知识库

会话附件接口前缀为 `/api/sessions/{session_id}`：

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/attachments` | 列出该会话直接附件 |
| POST | `/attachments` | multipart `files`，返回 202 和附件数组，后台解析/索引 |
| GET | `/attachment-types` | 返回 `{"extensions":[...]}` |
| GET | `/attachments/{file_id}` | 详情，含解析 metadata |
| DELETE | `/attachments/{file_id}` | 软删除、删向量和派生数据 |

知识库接口：

| 方法 | 路径 | 请求/返回 |
| --- | --- | --- |
| GET | `/api/knowledge-bases` | 知识库数组 |
| POST | `/api/knowledge-bases` | `{"name":1..120,"description":<=1000}`，201 |
| PATCH | `/api/knowledge-bases/{id}` | 同创建字段，返回更新对象 |
| DELETE | `/api/knowledge-bases/{id}` | 删除库及文档 |
| GET | `/api/knowledge-bases/attachment-types` | 支持扩展名 |
| GET | `/api/knowledge-bases/{id}/documents` | 文档及 metadata/状态 |
| POST | `/api/knowledge-bases/{id}/documents` | multipart files，202，异步处理 |
| DELETE | `/api/knowledge-bases/{id}/documents/{attachment_id}` | 删除文档及索引 |
| GET | `/api/knowledge-bases/{id}/documents/{attachment_id}/versions` | 同逻辑文档版本链 |

附件状态为 `uploaded/processing/ready/failed/deleted`。公开 payload 不返回 storage_key。

## 6. 审批

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/approvals?session_id=` | 待审批；返回 approval_id、tool_name、arguments、risk_level、session/run/tool_call、approval_batch_id |
| POST | `/api/approvals/batches/{batch_id}/respond` | body `{"decisions":{"id":"approved|denied|cancelled"}}`，必须覆盖整批 |
| GET | `/api/approvals/logs?session_id=&limit=&offset=` | 已完成审批日志 |
| GET | `/api/approvals/stats` | today_total/today_approved/today_denied/approval_rate |

审批决定进入 command queue；接口不直接恢复图。工具批次在 graph interrupt 中校验 batch_id 和完整 decisions，错误为 400/409。

## 7. 工具、MCP、Provider、Settings

### 工具

`GET /api/tools` 返回 `items` 和统计 `total/enabled/high_risk/calls_today`。每个 item 有 `name/description/risk_level/execution_mode/require_approval/enabled/parameters/last_called_at`。

`PATCH /api/tools/{name}` 支持部分字段 `enabled`、`risk_level(low|medium|high)`、`require_approval`；不能空 PATCH；内存状态和 tools 表同步。未注册为 404 `tool_not_registered`。

### MCP

`GET /api/mcp/servers` 返回 `items/total`；每项包含 name、command、args、掩码 env、status、tool_count、error、created_at。`POST /api/mcp/servers` 请求为 `{"mcpServers":{"name":{"command":string|[string],"args":[],"env":{},"image_id":null,"network_policy":"none","enabled":true}}}`。逐台尝试，单台失败不影响其它，始终 HTTP 200，返回 total/registered/failed/results。`DELETE /api/mcp/servers/{name}` 移除工具、断开连接并软删除；不存在 404。

### LLM Provider

`GET /api/providers` 只返回 name/provider/model/base_url/api_key_configured/api_key_masked/temperature/max_tokens。`POST /api/providers/test` 请求 provider/model/api_key/base_url，构造一次性 Provider 调用 `ping`，10 秒超时，返回 `ok/latency_ms/error`，不写配置。

### Settings

`GET /api/settings` 返回监听、PostgreSQL（不含密码）、embedding、Harness、LLM retry、记忆、压缩和审批设置；永不返回 API key、密码或 MCP env。

## 8. 记忆和检索 API

`POST /api/memory/search` 请求 `{"query": string, "where": object|null, "n_results": 5}`，返回记忆结果数组；检索失败转 500。`GET /api/memory?limit=50&offset=0&expired=false&session_id=` 返回 `items` 加 `count/expired_count/recent_count` 统计。`GET /api/memory/{id}` 返回正文和 metadata；不存在 404。`GET /api/memory/{id}/revisions` 返回 revision 链。

`GET /api/retrieval/runs` 支持 `limit<=100/offset/scope/status/session_id/agent_run_id/q`，返回 `items/total/limit/offset`；`GET /api/retrieval/runs/{run_id}` 返回配置、候选轨迹、分数、过滤原因和最终选中状态，不存在 404。
