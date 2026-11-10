# Athena v2 前端实施计划

状态：技术栈已确认；阶段 1–5 已完成实现与独立评审验收。

## 1. 当前状态

- 后端已提供认证、项目、Session、消息、Run、SSE、知识库和管理员配置 API。
- 生产前端工程 `frontend/` 已完成阶段 1–5。阶段 2 接入真实项目/Session
  目录、项目树、新对话项目上下文、项目中心四 Tab、Session
  分页/改名/删除，以及项目改名/删除冲突处理。
- 阶段 3 已接入 `/chat/:sessionId` 的消息历史、安全 Markdown、Run 提交与
  SSE 运行态、停止、补充输入、工具审批、失败重试和执行轨迹；920px/390px
  已覆盖执行轨迹抽屉与输入区。
- 阶段 4 已接入真实知识库目录、知识库详情、文档与版本管理、来源下载、检索
  测试和项目知识库关联；已完成独立评审，并通过前端自动化测试与生产构建。
- 阶段 5 已接入真实模型版本、上下文压缩策略、MCP Server 和工具审批策略管理；
  已完成统一管理员权限失效处理、全局错误边界、920px/390px 响应式检查、
  Vitest 回归和 Playwright 管理端核心链路验证，并已完成独立复审验收。
- 新对话继续只保留本地草稿，首次发送时才创建数据库 Session；Session 已创建
  但 Run 结果不明时进入该 Session 并保留原文和同一 `request_id`。
- 静态原型已经覆盖登录、对话、项目、知识库、管理控制台和主要运行态，但数据与交互均为示例。
- 前端和 API 采用同源部署；当前后端不提供 CORS。
- 登录态使用 HttpOnly Cookie；前端不能读取或持久化原始登录 token。
- Run SSE 以 PostgreSQL 快照为权威，`text_delta` 只是临时增量；断线后必须重新对账。
- 会话存在活跃 Run 时，新提交返回 `409 run_already_active`。
- 取消接口返回当前状态；返回 `cancelling` 时不能提前显示为已取消。
- 认证续期目前只有设计文档和后端规划，没有可调用的专用续期接口，不纳入首轮前端接入。

## 2. 目标与非目标

### 目标

- 将已确认的静态视觉方向落地为可运行的 `frontend/` Web 应用。
- 所有业务页面只展示真实后端字段，不保留静态原型里的虚构项目、成员、文档或运行事件。
- 完成登录、项目/会话导航、对话提交、Run 状态、SSE、停止、失败重试、审批和补充输入的主要闭环。
- 接入现有知识库和管理员接口。
- 保持桌面端视觉基线，并为移动端保留可继续完善的响应式结构。

### 非目标

- 不修改现有后端 API、数据库结构或 SSE 事件协议，除非后续确认独立需求。
- 不引入跨域部署或新的认证方式。
- 不把静态原型中的成员、项目说明、文档数量等后端不存在的数据带入正式页面。
- 不在本轮补齐邮箱验证、找回密码、设备管理或会话自动续期。
- 不把静态演示状态当成真实运行状态；生产页面不得硬编码 Run 进度。

## 3. 建议技术栈

| 领域 | 建议 |
| --- | --- |
| 框架 | React 18 + TypeScript |
| 构建 | Vite |
| 路由 | React Router |
| 服务端状态 | TanStack Query |
| HTTP | 原生 `fetch`，统一封装请求、错误和 401 处理 |
| 实时状态 | 专用 Run reducer/hook，消费原生 `EventSource` |
| 样式 | 沿用原型的 CSS 变量、布局和组件语言，不引入 UI 组件库 |
| 图标 | 优先沿用内联 SVG；需要扩充时使用统一图标组件 |
| 单元测试 | Vitest + React Testing Library |
| 端到端测试 | Playwright，当前覆盖管理端导航、表单和响应式核心链路 |

选择原生 `EventSource` 的原因：API 同源部署，浏览器会自动携带 Cookie；服务端已按
SSE `id` 支持自动重连，并在收到 `Last-Event-ID` 时先发送 `stream_interrupted`，
再发送权威快照。

## 4. 目录建议

```text
frontend/
  src/
    app/                 # 入口、路由、Providers、全局错误边界
    shared/
      api/               # fetch 封装、错误映射、DTO 类型
      lib/               # request_id、日期、SSE 解析等纯函数
      ui/                # 按钮、弹窗、状态标记等通用组件
      styles/            # tokens、reset、布局与共享样式
    features/
      auth/
      projects/
      sessions/
      chat/
      knowledge/
      admin/
    pages/
  tests/
```

每个 feature 内部保留自己的 API、hooks、组件和类型；跨 feature 复用必须通过
`shared/` 的明确公共入口，避免页面组件直接依赖其他 feature 的内部文件。

## 5. 路由与页面边界

| 路由 | 页面 |
| --- | --- |
| `/login` | 登录与开放注册 |
| `/chat` | 未绑定项目的新对话入口 |
| `/chat/new?project=<id>` | 继承指定项目的新对话 |
| `/chat/:sessionId` | 已存在会话，加载消息和当前 Run |
| `/projects` | 扁平项目列表 |
| `/projects/:projectId/overview` | 项目概览 |
| `/projects/:projectId/sessions` | 项目会话 |
| `/projects/:projectId/knowledge` | 项目关联知识库 |
| `/projects/:projectId/settings` | 项目设置 |
| `/knowledge` | 知识库总览 |
| `/knowledge/:baseId` | 知识库文档与检索 |
| `/admin/models` | 模型与压缩策略 |
| `/admin/mcp` | MCP Server |
| `/admin/tools` | 工具审批策略 |

项目中心的四个 Tab 使用独立 URL，刷新、返回和分享时保留当前 Tab。

阶段 3 的 `/chat/:sessionId` 已加载消息、最新 Run、Run 详情和工具调用，
并根据运行状态接入现有 SSE、停止、补充输入、审批和失败重试。

## 6. API 接入范围

| 功能 | 已存在的接口 |
| --- | --- |
| 当前用户与认证 | `/api/v1/auth/register`、`/login`、`/logout`、`/me`、`/password` |
| 项目 | `/api/v1/projects`、`/api/v1/projects/{id}` |
| 项目知识库关联 | `/api/v1/projects/{id}/knowledge-bases` |
| 会话 | `/api/v1/sessions`、`/api/v1/sessions/{id}` |
| 会话消息 | `GET /api/v1/sessions/{id}/messages` |
| Run | `/api/v1/sessions/{id}/runs` 及详情、取消、重试、审批、输入回答 |
| Run SSE | `GET /api/v1/sessions/{id}/runs/{runId}/stream` |
| 知识库 | `/api/v1/knowledge-bases` 及文档、版本、来源和检索接口 |
| 模型与压缩策略 | `/api/v1/admin/llm/...` |
| MCP 与工具 | `/api/v1/admin/mcp/...`、`/api/v1/tools`、`/api/v1/admin/tools` |

所有写请求继续依赖同源策略和 HttpOnly Cookie，不使用 `localStorage` 保存认证 token。

## 7. 对话运行状态与 SSE

### 初次打开会话

1. 并行读取 Session、最近 20 条消息和最新 Run。
2. 最新 Run 已知后读取 Run 详情、当前待审批项、待输入项和工具调用；这些
   请求必须依赖真实 Run ID，不伪造占位运行。
3. 仅 queued/running/cancelling 的 Run 建立 SSE；waiting 和终态由 Run 查询
   承载，避免建立马上结束的连接。
4. 首条 `snapshot` 到达后，以快照中的正式状态和消息覆盖临时展示。
5. 等待态或终态出现时立即关闭连接；同一 Run 重新 queued/running 后自动重连。

### 增量事件

- `snapshot`：权威状态和正式消息。
- `text_delta`：只更新临时流式文本，不直接写入正式历史。
- `run_event`：触发本地状态投影；必要时重新读取 Run 详情。
- `stream_interrupted`：保留已显示文本，进入“重新同步中”，等待后续快照。
- `heartbeat`：只用于确认连接仍然存活，不改变业务状态。

### 提交与停止

- 每次提交和命令生成独立 `request_id`；网络失败、5xx 或无法解析成功响应等
  结果不确定场景重试时复用该 ID，已明确拒绝的请求下一次使用新 ID。
- `409 run_already_active` 时锁定发送并展示原因，不丢弃用户草稿。
- 停止请求发出后进入 `cancelling`；只有服务端返回终态才显示“已取消”。
- 断线后不得清空已经显示的回答；收到权威快照后再校正文本。
- 审批、补充输入、失败重试和取消均使用后端返回的最新 Run 状态更新界面。

## 8. 分阶段交付

### 阶段 1：工程基础与认证

- 创建 `frontend/`、Vite 配置、TypeScript 配置和开发代理。
- 提取原型 CSS tokens 和基础组件。
- 实现路由外壳、登录、注册、当前用户加载、路由守卫和统一 401 处理。
- 建立 Vitest 配置和基础构建检查。

验收：

- 未登录访问业务路由跳转登录页。
- 登录成功后进入对话页，刷新后仍可通过 Cookie 恢复登录态。
- 退出后回到登录页，不能通过浏览器返回恢复受保护内容。
- 生产构建无 TypeScript 错误。

### 阶段 2：项目与会话目录

状态：已完成评审验收。

- 接入项目列表、创建、改名、删除及占用冲突提示。
- 接入 Session 列表、筛选、创建、改名、删除。
- 实现项目树展开/收起、最近记录、未归属项目分组和新对话项目绑定。
- 实现项目中心四个独立 URL Tab。

验收：

- 点击项目名称只展开/收起，多个项目可同时展开。
- 项目中心点击项目直接进入概览。
- 首条消息前可更改项目绑定，发送后锁定。
- 所有页面只展示后端已有字段。

### 阶段 3：对话与 Run 运行态

状态：已完成评审验收。

- 加载历史消息和 Markdown 内容，处理安全渲染。
- 提交 Run、展示临时流式文本、正式快照和工具调用轨迹。
- 接入停止、失败重试、审批和补充输入。
- 处理断线重同步、终态恢复和重复刷新。
- 新草稿首次发送才创建 Session；已有 Session 直接提交 Run，并对 Session
  创建成功但 Run 结果不明的情况保留原文和请求 ID。
- 所有失败路径保留用户输入，并映射 Run/审批/重试/网络等真实后端错误。

验收：

- 一次提交能展示 queued、running、waiting、cancelling 和终态。
- SSE 断开后保留已显示内容，并在重连后使用快照校正。
- `cancelling` 不会被提前显示成 `cancelled`。
- 活跃 Run 下再次提交得到明确冲突提示且草稿不丢失。
- 工具审批、补充输入或重试成功后刷新 Run 并继续连接同一 Run 或新 Run。
- `npm run typecheck`、`npm run test` 和 `npm run build` 通过；自动化测试
  使用 mock HTTP/FakeEventSource，不替代真实 Worker/LLM 和 Playwright 验收。

### 阶段 4：知识库

状态：已完成评审验收。

- 知识库列表、创建、改名、删除。
- 文档上传、文本导入、处理状态、版本、来源和重试。
- 项目知识库关联和解除关联。
- 检索结果及来源片段展示。

实施说明：

- `/knowledge` 使用真实知识库分页列表，`/knowledge/:baseId` 展示知识库详情。
  知识库侧栏复用同一真实目录，支持新建和当前项高亮，不实现展开树或目录图标。
- 知识库字段严格使用 `id/name/description/created_at/updated_at`；文档字段使用
  `id/knowledge_base_id/title/active_version_id/pending_version_id/created_at/updated_at`。
  知识库和文档列表均无 `total`，分页仅依据返回的 `items` 与 `page_size`。
- 知识库 CRUD 使用 `/knowledge-bases`。已有说明不能通过当前后端清空；编辑表单
  明确提示该限制，只允许设置或替换非空说明。
- 文档支持上传、文本导入、改名、删除、历史版本、失败重试，以及上传/粘贴文本
  创建新版本。上传入口按后端解析器支持 TXT、MD、PDF、DOCX、XLSX、PPTX、
  PNG、JPG、JPEG；前端校验 20 MiB、255 字符标题和 1,000,000 字符文本，后端
  错误仍为权威。
- 最新版本状态从 `GET .../versions` 读取。`queued/processing` 每 3 秒轮询，
  `ready/failed` 停止；页面不展示百分比或后端未提供的文件类型、大小和页数。
  失败替换会说明已有活动版本是否仍可检索，且仅在最新版本失败且没有待处理版本
  时提供重试。
- 上传和文本导入使用 `newRequestId` 生成 `Idempotency-Key`。网络错误、5xx 和
  `invalid_response` 保留表单与同一请求键；后端明确拒绝或成功后换新键，表单
  内容变化也换新键。
- 来源下载通过鉴权同源请求访问
  `GET .../versions/{versionId}/source`，解析 `Content-Disposition` 的
  `filename*` 后回收 Blob URL，不拼接 MinIO 地址。
- 检索测试默认 `limit=8`，支持 1–20；结果展示真实 hits 的标题、纯文本、通道
  和定位信息，可继续读取完整片段，并展示 `degraded_reasons`、空结果和
  `knowledge_search_unavailable` 状态。
- 项目知识库 Tab 使用真实关联/解除接口，选择弹窗排除已关联项并显示
  “已关联 N / 20”。达到上限时禁用新增；后端返回
  `project_knowledge_limit_reached` 时展示冲突并重新读取关联列表。
- 统一映射知识库、文档、版本、来源、幂等、处理中、格式、大小、配额、存储、
  检索、项目上限、404 和网络错误，不直接把响应正文当作界面文案。

验收：

- 上传和导入显示真实处理状态。
- 来源下载使用后端鉴权接口，不拼接 MinIO 地址。
- 项目最多关联 20 个知识库的边界由后端错误和前端状态共同体现。
- `npm run typecheck`、`npm run test` 和 `npm run build` 通过。自动化测试使用
  mock HTTP，不替代真实 Worker/OCR/MinIO/PostgreSQL 检索和 920px/390px
  真实浏览器验收。

### 阶段 5：管理控制台与收尾

状态：已完成实现与独立评审验收。

- `/admin/models` 使用真实模型版本和压缩策略接口。模型版本只允许创建不可变
  版本、启停和设默认，不提供编辑或删除；页面仅展示后端安全投影，只显示 API Key
  配置状态。默认入口仅对 `enabled && supports_stream &&
  supports_tools` 的版本开放；停用当前默认版本需要二次确认，并明确后端不会
  自动切换、新 Run 可能失败。
- 模型创建表单校验 HTTPS `base_url`、API Key、上下文/输出关系和并行工具调用依赖，并映射 403/409/422。压缩策略要求
  `0 < 比例 < 1`、`recent_ratio + summary_ratio < 1`，压缩模型必须已启用；
  没有后端关闭开关，未配置时显示真实空态。
- `/admin/mcp` 使用真实分页和 `total`，展示 transport、配置、
  `config_version`、秘密名称及 `has_value`、发现时间等后端字段。创建和编辑按
  transport 切换并校验 HTTPS URL、stdio 绝对路径、秘密名和 16 KiB 限制。
  编辑是完整替换，必须重新输入全部秘密；更新携带
  `expected_config_version`，409 后立即锁空旧版本。刷新成功后以最新详情重置
  整份配置和秘密名称、清空全部秘密值，明确提示旧草稿未保存并要求重新输入；
  刷新失败则保持提交锁定并要求关闭重开。创建/更新成功会同时失效 Server 与
  工具目录。等待期间明确展示“正在连接并发现工具”，不伪造连接成功，也不提供
  后端不存在的“重新发现”按钮。
- `/admin/tools` 使用真实分页和 `total`，仅展示后端已注册 MCP 工具的
  `tool_id/model_name/description/server_id/remote_name/requires_approval`。
  审批开关逐工具提交，提交期间锁定全部开关，失败时回滚。后端没有批量接口，
  页面不提供批量操作。
- 管理接口返回 403 `admin_required` 时，统一发布管理权限失效事件：移除全部
  管理查询缓存，重新核验 `GET /auth/me`，路由显示无权限状态，不回退保留旧
  管理数据。全局 `ErrorBoundary` 包住 Query/Auth Providers 和路由，兜底页面
  不回显原始异常文本。
- 三个管理页沿用现有设计令牌和布局，覆盖桌面、约 920px 和 390px 视口；
  表单 label、ARIA 错误关联、异步 `role=status`、开关语义和键盘焦点均按现有
  组件模式实现。
- Playwright 已配置并安装 Chromium，覆盖管理子导航、模型创建、MCP STDIO
  表单、逐工具 PATCH，以及三个管理页在 920px/390px 下无水平溢出。E2E 使用
  `page.route("**/api/v1/**")` 的确定性 HTTP fixture，只验证前端契约和交互，
  不代表真实 LLM、MCP 连接/发现或后端数据库闭环。

验证结果：

- `npm run typecheck` 通过。
- `npm run test`：27 个测试文件、181 项测试通过。
- `npm run build` 通过；Vite 报告主 JS chunk 超过 500 kB 的非阻断体积警告。
- `npm run test:e2e`：5 项 Playwright 测试通过（HTTP fixture）。

## 9. 已决事项与剩余待确认

已决事项：

- React 18 + TypeScript + Vite + React Router + TanStack Query 的技术栈已确认。
- 阶段 1–5 已完成实现与独立评审验收。

剩余待确认：

- 生产环境采用 FastAPI 托管构建产物还是由反向代理托管，留到部署阶段决定；
  开发阶段继续使用 Vite 同源代理。
