# Athena Frontend

Athena 的生产前端。阶段 1–5 已完成实现与独立评审验收：在真实认证、项目、
Session、对话运行态和知识库之上，已接入模型版本、上下文压缩策略、MCP Server
和工具审批策略管理。页面只使用现有后端契约，不伪造 LLM/MCP 连接结果。

技术栈为 React 18、TypeScript、Vite、React Router、TanStack Query，以及
Vitest + React Testing Library。Markdown 使用 `react-markdown` 和
`remark-gfm`；样式沿用仓库原型提炼的 CSS 变量与原生组件，没有引入 UI 组件库。

## 环境要求

- Node.js 20 或更高版本（推荐当前 LTS）
- npm
- 可运行的 Athena 后端，默认地址 `http://127.0.0.1:8000`

后端启动、PostgreSQL 和迁移说明见仓库根目录的 `README.md`。

## 安装与开发

```sh
cd frontend
npm install
npm run dev
```

Vite 默认从 `http://localhost:5173` 提供页面，并把 `/api` 代理到
`http://127.0.0.1:8000`。代理保留浏览器原始的 Host 与 Origin，因此后端
现有的同源写请求校验可以直接工作。

请通过 Vite 地址访问前端，不要把开发页面单独托管到另一个 origin。后端目前
不提供 CORS，生产环境的同源静态托管或反向代理方案留到后续部署阶段确认。

如果后端未运行：

- 页面会显示“暂时无法确认登录状态”，可启动后端后重试。
- 登录表单和运行命令会显示安全的连接错误，不会伪造本地成功状态。

## 认证边界

- 登录态只依赖后端签发的 HttpOnly Cookie，名称为 `athena_session`。
- 所有请求统一经过 `src/shared/api/client.ts`，使用
  `credentials: "same-origin"`。
- 前端不读取、保存或从 `localStorage` 恢复原始登录 token。
- 页面加载时调用 `GET /api/v1/auth/me` 恢复会话。
- 任意 API 返回 `401` 时，客户端发布 `athena:auth-unauthorized` 应用事件，
  取消在途的当前用户查询，取消并移除除 `auth` 外的全部查询缓存，认证缓存置为
  匿名状态，受保护路由随后跳转登录页。
- 登录、注册、退出和全局 `401` 共用身份边界清理：先推进前端身份代次，再
  取消/移除旧业务查询，最后写入新用户或匿名状态。旧身份迟到响应因代次不匹配
  被通用请求层丢弃；JSON、错误详情和 Blob 响应体读取完成后都会再次校验，
  不会回写缓存，也不会用陈旧的 `401`/`403` 事件影响新用户。
- 工具审批的乐观 mutation 会在点击时记录身份代次；`onMutate` 取消查询后和
  `mutationFn` 发请求前都会校验，身份变化时在发送 PATCH 前中止。迟到的成功、
  失败和 settled 回调也不会写入、回滚或失效任何缓存。
- 管理 API 返回 `403 admin_required` 时，客户端发布
  `athena:admin-forbidden` 事件；应用移除全部管理查询缓存，重新调用
  `GET /api/v1/auth/me`，并在权限不再有效时显示无权限页。该分支只清管理根
  缓存，不扩大为全局身份清理。

## 当前路由

| 路由 | 当前行为 |
| --- | --- |
| `/chat` | 未归属项目的本地新对话草稿；首次发送才创建 Session |
| `/chat/new?project=<id>` | 继承指定项目的本地新对话草稿 |
| `/chat/:sessionId` | 完整对话工作台：历史消息、Run、SSE、审批、补充输入与轨迹 |
| `/projects` | 真实项目列表、空态和新建入口 |
| `/projects/:projectId/overview` | 项目真实时间和 Session 概览 |
| `/projects/:projectId/sessions` | 真实分页 Session、改名和删除 |
| `/projects/:projectId/knowledge` | 真实项目知识库关联、解除和 20 个上限 |
| `/projects/:projectId/settings` | 项目改名、时间和删除边界 |
| `/knowledge` | 真实知识库分页、创建、改名和删除 |
| `/knowledge/:baseId` | 文档、版本、来源下载与检索测试 |
| `/admin` | 重定向到模型管理 |
| `/admin/models` | 模型版本、默认模型和上下文压缩策略 |
| `/admin/mcp` | MCP Server 真实目录、创建和完整替换编辑 |
| `/admin/tools` | 已注册 MCP 工具的逐项审批策略 |

未知项目 Tab 会回到 `overview`；项目不存在、已删除或不属于当前用户时显示
明确的不可访问状态。Session 分页使用 `?page=`，刷新后保持当前页。

## 对话与 Run

- 打开 `/chat/:sessionId` 时并行读取 Session、最近 20 条消息和最新 Run；
  最新 Run 已知后再读取详情与工具调用，活跃 Run 连接
  `GET /api/v1/sessions/{sessionId}/runs/{runId}/stream`。
- 消息使用 `GET /api/v1/sessions/{sessionId}/messages?limit=20&before_sequence=`，
  可继续加载更早消息；所有分页与 SSE 快照按 `sequence` 合并去重，当前 Run
  的权威快照覆盖同序号历史文本。
- Markdown 只展示后端文本内容，禁用原始 HTML，支持 GFM；HTTP(S) 外链使用
  `target="_blank"` 和 `rel="noopener noreferrer"`，图片不自动加载，只降级为
  安全链接。
- 新草稿首次发送先调用 `POST /api/v1/sessions`，成功后调用
  `POST /api/v1/sessions/{id}/runs`。如果 Session 已创建但 Run 结果不明，
  页面会进入该 Session，保留原文与同一 `request_id`，不会再次创建 Session。
- `/chat` 草稿不会提前创建空数据库 Session；标题保持后端原值，空标题显示
  “未命名对话”，前端不会自动生成标题。
- Run 状态覆盖 `queued`、`running`、`waiting_for_input`、
  `waiting_for_approval`、`cancelling`、`completed`、`failed` 和 `cancelled`。
  `cancelling` 只在服务端返回终态后变为“已取消”。
- SSE 使用原生 `EventSource`：`snapshot` 是权威覆盖，`text_delta` 只追加临时
  文本，`run_event` 触发查询对账，`heartbeat` 只保活，
  `stream_interrupted` 保留文本并提示重新同步。等待态和终态主动关闭连接；
  Run 重新 queued/running 后自动重连。组件卸载和切换 Run 也会关闭旧连接。
- 活跃 Run 下输入区保留草稿但锁定重复提交，并提供停止按钮。停止可重复请求，
  界面始终显示服务端返回的真实状态。
- 等待补充输入时，使用 `pending_input.id` 回答当前问题；展示的问题文本来自
  对应真实消息，未加载到历史窗口时明确提示而不伪造问题。
- 等待工具审批时，逐项展示服务端 `pending_approvals` 的调用记录 ID、
  `model_name`、`tool_kind`、序号和时间，并通过工具详情端点读取参数；
  approve/reject 使用同一调用记录 ID。最后一个决定使 Run 重新排队时，页面
  自动连接同一 Run 的新 SSE。
- 仅最新 failed Run 显示重试入口；确认弹窗明确说明从头执行和副作用重复风险，
  确认后发送 `confirm_unknown=true`，并切换到新 Run。
- 执行轨迹只展示真实工具调用、状态、耗时、arguments 和 outcome；桌面可收起，
  920px 与 390px 视口可使用抽屉式面板和输入区。

## 项目与 Session 数据

- 所有业务请求复用 `src/shared/api/client.ts` 的 `apiRequest`，保留
  `credentials: "same-origin"`、HttpOnly Cookie 和统一 401 行为。
- 项目只展示后端 DTO 的 `id`、`name`、`created_at` 和 `updated_at`。
- Session 只展示后端 DTO 的 `id`、`title`、`project_id`、`created_at` 和
  `updated_at`；空标题统一显示为“未命名对话”。
- 项目目录会逐页读取当前用户可见项目；对话侧栏只在项目展开时读取前 5 条
  Session，最近列表最多读取 8 条，未归属分组只在后端 `total > 0` 时出现。
- TanStack Query 的 key 与失效策略集中在各 feature 的 `queryKeys.ts`。
  项目和 Session 写操作会刷新目录；Run 事件和命令结果会刷新最新 Run、
  Run 详情、消息和工具调用缓存。
- 每次提交、回答、审批和重试都使用 `request_id`。网络失败、服务端 5xx 或
  成功响应无法解析时，重试复用原 ID；已明确拒绝的请求下一次使用新 ID，
  避免永久冲突。
- 所有失败路径保留用户输入；错误文案覆盖 Run 冲突、输入请求失效、审批失效、
  不可重试、会话不存在和网络错误等后端详情。

## 知识库

- 知识库只展示后端字段 `id/name/description/created_at/updated_at`，侧栏复用
  真实管理式列表，不使用展开树或文件夹图标。
- 文档支持文本导入、文件上传、改名、删除、历史版本、失败重试和替换新版本。
  上传入口只接受后端解析器支持的 TXT、MD、PDF、DOCX、XLSX、PPTX、PNG、
  JPG、JPEG；前端校验 20 MiB、255 字符标题和 1,000,000 字符文本，后端错误
  仍为权威。
- 最新版本状态通过 `GET .../versions` 读取，queued/processing 每 3 秒轮询，
  ready/failed 停止；不展示百分比或后端未提供的文件大小、页数等信息。
- 来源文件通过同源 Cookie 请求鉴权下载接口，解析 `Content-Disposition` 的
  `filename*` 后创建并回收 Blob URL，不拼接 MinIO 地址。
- 检索测试默认 `limit=8`，支持 1–20；展示真实 hits、纯文本片段、channels、
  locators、完整 chunk 和 `degraded_reasons`，并区分空结果、
  `503 knowledge_search_unavailable` 与降级状态。
- 项目知识库最多关联 20 个；关联弹窗排除已关联项，后端返回
  `project_knowledge_limit_reached` 时刷新关联列表并如实提示冲突。

## 管理控制台

- `/admin/models` 只允许创建不可变模型版本、启停和设默认，不提供编辑或删除。
  默认版本必须 `enabled && supports_stream && supports_tools`；停用当前默认
  版本需二次确认，页面不会自行切换默认模型。API Key 只在创建时输入并加密保存，
  页面只显示配置状态。升级时无法恢复旧环境密钥，旧模型会停用、默认指针和当前压缩策略会清除；管理员需重新录入模型密钥并保存压缩策略。压缩策略与模型版本同页管理，比例为 `0 < v < 1` 且近期原文与摘要
  比例之和小于 1。
- `/admin/mcp` 使用真实分页和 `total`，展示 transport、配置、
  `config_version`、秘密名称及 `has_value`、发现时间。编辑是完整替换，必须
  重新输入全部秘密；未重输或显式移除的旧秘密会被删除。更新携带
  `expected_config_version`，409 后禁止继续使用旧版本提交并读取最新详情。
- MCP 创建和完整替换会由后端真实连接并发现工具；提交期间表单锁定并明确显示
  等待状态。后端没有重新发现接口，页面不提供伪按钮，也不宣称连接成功。
- `/admin/tools` 只展示后端已注册 MCP 工具。审批开关逐工具提交，提交期间
  锁定全部开关，失败回滚；后端没有批量接口，页面不提供批量操作。
- 管理页在桌面、约 920px 和 390px 视口下无水平溢出，表单错误使用
  `aria-invalid`/`aria-describedby`，异步状态使用 `role=status`。全局错误
  边界包住 Providers 和路由，兜底页不回显原始异常。

## 常用命令

```sh
npm run typecheck
npm run test
npm run test:watch
npm run build
npm run test:e2e
npm run preview
```

`npm run build` 会先执行 TypeScript 严格检查，再生成 `frontend/dist/`。
当前后端尚未配置为托管该构建产物。

首次运行 Playwright 前需要安装 Chromium：

```sh
npx playwright install chromium
```

## 测试

自动化测试使用 Vitest、jsdom 和 React Testing Library；HTTP 与
`EventSource` 均使用 mock/fake，不依赖真实网络或数据库。覆盖范围包括：

- 认证、401、A→B 账号切换、响应体延迟、旧身份迟到响应与审批请求隔离、
  项目/Session 目录、查询参数、错误码和已有弹窗交互。
- Session、消息和最新 Run 的并行加载，历史分页及按 sequence 合并去重。
- 安全 Markdown：原始 HTML 不执行、协议白名单、外链安全属性、图片降级。
- queued → running → waiting → cancelling → 终态投影；
  `cancelling` 不提前显示 cancelled。
- `snapshot` 校正 `text_delta`、`stream_interrupted` 保留文本、heartbeat、
  断线提示、终态关闭、重新 queued 重连和卸载关闭。
- 提交冲突保留草稿、网络失败复用 request_id、已知冲突改用新 ID。
- 新草稿创建 Session 后的部分失败兜底，不重复创建 Session。
- 补充输入、工具审批、失败重试风险确认，以及成功后继续连接最新 Run。
- 920px/390px 执行轨迹抽屉与 composer 输入可用。
- 模型创建/默认/停用确认、压缩比例约束，MCP 409 与完整替换秘密，工具逐项
  提交失败回滚，管理员 403 清缓存、分页纠偏、空态和真实字段。
- 知识库 CRUD、文档上传/导入与版本轮询、来源下载、检索降级与项目关联上限。

Vitest 测试不证明真实 Worker、LLM、MCP 或生产部署闭环。Playwright 已配置
Chromium，`e2e/admin.spec.ts` 使用 `page.route("**/api/v1/**")` 的确定性
HTTP fixture，覆盖管理导航、模型创建、MCP STDIO 表单、单工具 PATCH 和
920px/390px 无水平溢出；它验证前端契约与交互，不是真实 LLM/MCP 连接、发现、
超时、数据库或后端集成验收。

本阶段另使用隔离 PostgreSQL/Redis 和临时后端完成了 HTTP+SSE 快照 smoke：
验证 Session/Run 创建、消息与 Run 查询、SSE `snapshot`、同 `request_id`
重放不重复创建 Run，以及重复取消和终态流关闭。该 smoke 未启动 Worker/LLM，
因此不构成真实问答、工具审批或生产闭环证据。

## 目录

```text
frontend/
  src/
    app/         # 入口、Providers、路由、受保护应用外壳
    features/
      auth/      # 认证 API、缓存、表单、守卫和测试
      chat/      # 对话工作台、消息、Run、SSE、审批、轨迹和草稿上下文
      knowledge/ # 知识库、文档、版本、来源下载和检索
      admin/     # 模型、压缩策略、MCP Server 和工具审批
      projects/  # 项目 API、目录、四 Tab 和项目中心
      sessions/  # Session API、标签、缓存和表单弹窗
    pages/       # 仅保留跨域级无权限等少量页面
    shared/      # fetch 封装、格式化、通用弹窗/空态/错误条/错误边界和样式
    test/        # Vitest 初始化、fixtures、FakeEventSource 和测试渲染工具
  e2e/           # Playwright 管理端 HTTP fixture 测试
```

## 当前边界

- `/chat/:sessionId` 已接入真实消息、Run、SSE、停止、补充输入、审批、重试和
  工具调用轨迹；不展示静态原型中的虚构步骤或运行事件。
- 对话页不提供普通附件；文件上传仅用于知识库文档和替换版本。
- 不提供项目管理之外的成员权限、MCP 批量审批或虚构的 MCP 重新发现入口。
- 不提供邮箱验证、找回密码、设备管理或自动续期入口。
- 没有前端自动标题生成，也没有对后端执行状态的本地伪造或轮询替代 SSE。
- 生产同源托管、真实 Worker/LLM/MCP 端到端联调尚未完成；Playwright 当前只
  使用 HTTP fixture。构建仍有主 JS chunk 超过 500 kB 的非阻断体积警告。
