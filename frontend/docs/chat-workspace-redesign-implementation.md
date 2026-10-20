# Athena 聊天工作台改版实施方案

## 1. 文档目的

本文把 `athena-chat-proposal.html` 静态稿转换为可按阶段实施、检查和验收的前端方案。第一阶段只调整聊天工作台的信息结构和视觉呈现，沿用现有 API、SSE 事件、Zustand 状态和业务动作；不改后端协议，不重做管理页面。

## 2. 当前基线

- 前端使用 React 18、TypeScript、Vite、Tailwind CSS、Zustand 和 lucide-react。
- `App.tsx` 负责认证、视图切换、会话选择和页面布局。聊天视图当前由 `NavRail`、`Sidebar`、`StatusIndicator`、`Chat` 组成。
- `Chat.tsx` 已处理历史消息合并、SSE 驱动的流式更新、消息阶段分组、附件上传/粘贴/拖放、停止运行、审批和操作时间线。
- `MessageBubble.tsx` 已负责 Markdown、工具调用结果、附件和消息时间。
- `ActivityPanel.tsx` 已读取请求组、当前编排计划和任务，但当前定位是请求索引，不能直接承担完整执行上下文展示。
- `chatStore.ts` 已保存消息、Agent 状态、附件、待审批项、执行时间线、编排计划和任务。`api/client.ts` 已提供会话、消息、附件、运行列表和批量审批接口。
- `deriveOrchestrationView`、`buildActivityGroups` 已有投影逻辑，实施时优先复用，避免 UI 组件重复推导业务状态。
- 当前工作树有大量未提交改动，改版必须基于执行时文件状态逐段编辑，不能还原或覆盖用户改动。

基线验证（2026-10-20）：在 `frontend/` 执行 `npm run typecheck` 与 `npm run build` 均通过。此结果只说明改版前构建可用，不替代改版后的验证。

## 3. 目标与非目标

### 目标

1. 让聊天成为清晰的工作区：会话导航、对话、执行状态、输入操作各自有稳定位置。
2. 用户能在消息上下文中理解 Athena 正在做什么、哪些任务完成、哪些任务等待确认。
3. 将当前运行摘要、任务状态、操作记录集中到可折叠的执行上下文面板。
4. 保留会话、消息、附件、审批、SSE 与运行控制的现有行为。
5. 桌面端使用三栏布局；中小屏使用抽屉/面板切换，输入区始终可达。
6. 保持组件可读和状态归属明确，避免把整页重写为单个大组件。

### 非目标

- 不新增后端 API、数据库字段、SSE 事件或权限策略。
- 不在本阶段改知识库、记忆、工具、审批日志、Provider、MCP、检索、评测和设置页面的布局。
- 不实现静态稿中的模型选择器或提示词库按钮。确认已有数据源和操作 API 后，另立范围。
- 不实现跨页面搜索会话、快捷键命令面板或会话分组；静态稿中的搜索外观不意味着本阶段具备搜索能力。
- 不为了匹配示例图创建静态假数据或推测后端字段。

## 4. 目标信息架构

### 桌面端（建议断点 >= 1200px）

```text
┌───────────────────┬────────────────────────────────────┬─────────────────────┐
│ 工作区/会话侧栏   │ 顶栏 + 消息流 + 固定输入区          │ 执行上下文面板      │
│                   │                                    │                     │
│ 新建会话          │ 当前会话标题 / 运行状态             │ 当前请求摘要        │
│ 现有管理页入口    │ 用户与 Athena 消息                  │ 任务进度            │
│ 最近会话列表      │ 任务进度、审批、时间线               │ 操作记录            │
│ 用户/连接状态     │ 附件和消息输入                       │ 可关闭/折叠         │
└───────────────────┴────────────────────────────────────┴─────────────────────┘
```

- 左栏宽度目标 224–256px；中间列 `minmax(0, 1fr)`；右栏目标 300–340px。
- 屏幕可用宽度不足约 1200px 时，右栏默认收起，由顶栏图标打开为覆盖式抽屉。
- 不把现有 11 个 `NavRail` 图标压缩成移动端横向滚动条。聊天视图用有文字标签的工作区导航；如需节省空间，可把低频管理入口归入“更多”，但本阶段需确保每个 `AppView` 仍可到达。

### 小屏（< 768px）

- 默认只显示聊天主区，底部固定输入区，顶部显示当前会话标题及会话列表/执行上下文入口。
- 会话列表与执行上下文使用独立抽屉；两者不可同时覆盖输入区。打开一个抽屉时关闭另一个。
- 触屏下提供明确的关闭按钮；支持 Escape 关闭、焦点回到触发按钮，并设置合理的 `aria-label` / `aria-expanded`。
- 管理视图沿用当前 `PageView` 与 `NavRail` 行为，不要求它们共享新的聊天侧栏。

## 5. 组件与文件改造清单

### `frontend/src/App.tsx`

- 将聊天专属工作区布局与非聊天管理页布局分支明确分开。
- 保留现有认证、会话初始化、SSE hook、`handleNewSession`、`handleDeleteSession`、`handleSelectSession`、改名和退出登录逻辑。
- 新聊天工作区侧栏需要接收现有会话回调和 `activeView`，不能复制会话状态或直接调用 API。
- 非 chat 视图继续使用现有 `NavRail` + `PageView`，避免范围扩张。
- 移动端抽屉开关可由工作区壳层持有，通过 props 交给 `Chat` 与侧栏；关闭/切换状态要在视图变化时复位。

### `frontend/src/components/NavRail.tsx` 与 `Sidebar.tsx`

- 提取/复用菜单项定义，确保导航标签、图标和 `AppView` 映射只有一份事实来源。
- 将现有两个聊天侧栏职责组合为工作区侧栏：品牌/新建会话、分组导航、最近会话、底部状态。
- 管理页中仍可保留当前 NavRail 呈现，或由同一菜单数据生成现有 Rail，避免导航项目丢失。
- Sidebar 目前会话项使用 `div onClick` 且删除没有确认。整合时建议会话选择使用可聚焦按钮；不在本改版内更改删除语义，以免顺带触碰破坏性操作流程。
- 会话长标题截断；重命名仍支持 Enter 保存、Escape 取消；图标按钮继续提供 title 和 aria-label。

### `frontend/src/components/Chat.tsx`

- 保留所有数据获取和交互副作用，不把流逻辑移入纯展示组件。
- 整理返回 JSX，形成顶栏/消息滚动区/Composer 的稳定纵向布局，避免滚动容器被右栏或抽屉意外重置。
- 当前请求运行状态继续通过现有 `activeTimelineEntries`、`orchestrationView`、`pendingApprovals` 推导；确保每条 timeline 和每个审批批次只显示一次。
- 将执行上下文面板状态提升到布局能协调的组件；桌面开关与移动抽屉共享同一状态。
- 保留“用户离开消息底部时不强制滚回”的 `shouldFollowLatestRef` 行为。
- 保留附件列表、待处理状态轮询、粘贴上传、拖放上传、发送禁用条件、停止命令和错误反馈。

### `frontend/src/components/ActivityPanel.tsx`（或新建 `ExecutionContextPanel.tsx`）

- 建议新建 `ExecutionContextPanel.tsx` 承担新面板语义，逐步复用 `ActivityPanel` 中的请求分组/状态图标；不要在旧组件内堆叠与现有职责相反的 UI。
- Props 至少包含 `messages`、`agentStatus`、`executionTimeline`、`orchestrationTasks`、`orchestrationPlans`、`attachments`、`onClose`。若组件直接订阅 store，则应说明状态边界并用 selector 降低无关重渲染；优先由父层组装所需数据。
- 运行摘要从当前 session 的最新有效 run/timeline 得出；任务从与该 run 的 plan 关联的任务中过滤。缺少 `run_id` 时退化为当前会话最新计划，并避免展示其他 run 的残留任务。
- 当前类型 `SessionRunSummary` 仅有 run id、session id、status、root thread id 和时间，不含 task 总数或 plan 状态。首期计数以 `deriveOrchestrationView` 的 `counts` 为准；无计划时显示“暂无并行任务”，不要伪造任务总数。
- 操作记录使用 `ExecutionTimelineEntry` 的 label、detail、status、timestamp；初始显示最近若干条（建议 5 条），支持展开全部。按 `session_seq` 优先排序，缺失时按 timestamp 排序；相同事件依 id 去重。
- 附件摘要只展示文件名、status、size 等已有字段；不推断解析百分比。

### `frontend/src/components/MessageBubble.tsx`

- 以 CSS/布局调整为主，保留 Markdown 渲染、表格处理、工具结果关联、附件和时间戳逻辑。
- 头像和角色标签应在窄屏保持稳定尺寸；长 URL、代码块、表格在自身容器横向滚动，不撑破消息列。
- 消息操作按钮（复制、反馈、重试）如果本阶段不接 API，应不展示，不能只留下外观按钮。

### `frontend/src/components/ApprovalDialog.tsx`

- 调整为与执行卡片视觉一致的紧凑内嵌卡片；继续显示工具名、风险等级、自然语言操作说明、可展开参数。
- 必须保留先选决策、再提交批次的语义。`respondApprovalBatch` 只在用户点击“提交决策”后调用；提交中禁用按钮，失败时恢复可提交状态。
- 一批多项审批需要逐项显示选择并提供“全部允许/全部拒绝”，不简化成一个未经区分的立即允许按钮。
- 审批成功后以 SSE `approval.resolved` 更新为准；请求返回不代表工具执行已经完成。

### `frontend/src/index.css`、`frontend/tailwind.config.js`

- 只添加聊天工作台需要的布局/状态样式；若必须改颜色 token，应确认所有已有页面不会被意外改色。
- 现有主题为深色，而静态稿为浅色主工作区/深色导航。建议首期通过聊天页面 scoped class 实现，不直接全局替换 `athena-*` 色板。
- 使用既有 Tailwind 和 lucide-react；不增加图标库、组件库或运行时依赖。
- 为 fixed composer、滚动区、窄栏、抽屉和高对比焦点状态定义明确尺寸，避免动态内容导致布局跳动。

## 6. 数据映射与选择规则

| 设计显示 | 现有来源 | 规则/空状态 |
|---|---|---|
| 会话标题、最近会话、状态 | `sessions`, `activeSessionId` | 直接复用 Sidebar props；不另存一份会话列表 |
| 连接状态 | `connectionStatus` | 复用 `StatusIndicator` 的状态语义；视觉可压缩 |
| Agent 运行状态 | `agentStatus`, `thinking` | 统一映射 idle/thinking/running/waiting_approval/paused/completed/error |
| 对话消息与时间 | `messages` | 保留现有日期分组和 run 关联，消息按原 timestamp 展示 |
| 并行任务摘要 | `orchestrationPlans`, `orchestrationTasks`, `deriveOrchestrationView` | 必须按当前 run/plan 过滤；无数据时显示空状态 |
| 审批请求 | `pendingApprovals` | 按 `approval_batch_id` 聚合，沿用 `handleApproval` |
| 操作记录 | `executionTimeline` | 当前 run 优先；过滤其他 run；排序、展开、长详情换行 |
| 附件数量与状态 | `attachments`, `Message.attachments` | 列表来源为 session attachments，单条消息来源为 attachment refs |
| 当前 run id/status | 最新 `executionTimeline` lifecycle + `listSessionRuns` 可选结果 | 不从会话 status 推断 run 的精确状态；无可靠 run id 时弱化展示 |
| 最后更新/总耗时 | message/timeline timestamps | 仅在数据存在且可解析时展示；不显示静态稿中的固定数字 |

注意：切换 session 时 `Chat` 会先清空并重新加载历史，同时 SSE replay 与 REST history 并发合并。新面板不得通过本地“最近任务”缓存绕过该清理，所有显示必须随现有 session 清理/订阅机制同步。

## 7. 状态与交互规格

### 会话侧栏

- 新建、切换、重命名、删除继续走 App 当前 handler。
- 导航点击调用 `setActiveView`；聊天入口回到当前会话。
- 没有会话、正在加载会话、会话请求失败应有独立状态，不显示静态示例会话。
- 移动端选择会话后关闭抽屉。

### 执行上下文

- 默认策略：宽桌面默认展开；中屏和移动端默认收起。
- 切换会话后面板内容随 store 清理；不能短暂显示上一个会话的计划/审批/事件。
- 空闲无消息时显示轻量空态；有消息无 run 时仅显示请求状态和操作记录，不强行出现 DAG 概念。
- 状态颜色同时配合文字/图标，不能只靠颜色区分。

### Composer

- Enter 发送，Shift+Enter 换行；保留已有 handler。
- 上传中、附件处理中、缺少会话、审批或运行状态的发送限制按现有 `canSend` 逻辑。
- 运行时显示停止按钮；停止后等待后端事件确认，不在前端伪造“已停止”。
- 模型选择、提示词、语音、联网等未接通功能不显示为可操作按钮。

### 审批

- 显示动作对象、工具和风险；技术参数可折叠查看。
- 决策选择和提交分离，防止单击误触产生外部写操作。
- 网络错误时显示可理解的错误且允许重新提交；重复提交被锁定。

## 8. 建议实施阶段

### 阶段 A：布局骨架

- 新增聊天工作区外壳/侧栏组件（建议 `components/chat/ChatWorkspace.tsx` 与 `ChatWorkspaceSidebar.tsx`，目录命名依仓库约定调整）。
- 在 App 中对 chat 与非-chat 视图分流；保留管理页现有 Rail。
- 建立 desktop grid、main scroll、composer 固定布局和 responsive drawer 状态。
- 验收：视图跳转、会话 CRUD 回调、桌面与手机侧栏开关正确；消息逻辑未改。

### 阶段 B：消息与审批视觉

- 按静态稿调整消息密度、头像、时间戳、附件片和执行状态卡片。
- 调整审批卡片皮肤，但保留选择后提交的现有流程。
- Composer 改为统一容器视觉，保留现有上传和发送逻辑。
- 验收：流式消息、工具结果、附件、审批批次和停止操作均可用；无占位假按钮。

### 阶段 C：执行上下文面板

- 新建或改造右栏，将请求摘要、plan/task 聚合、事件列表组合展示。
- 明确当前 run 选择与无 run fallback，修复任何跨 run 或跨 session 泄漏风险。
- 桌面右栏常驻/可隐藏，小屏抽屉打开关闭；长事件可展开。
- 验收：SSE 更新可见、审批 waiting 状态准确、时间线不重复、空状态正确。

### 阶段 D：收尾与质量检查

- 加入可访问名称、焦点环、Escape 关闭、键盘操作、足够的触控尺寸。
- 手动检查消息滚动跟随/离开底部、加载/错误/空状态和窄屏溢出。
- 运行 typecheck/build；按改动面执行针对性测试。
- 验收：无控制台 React key/受控输入警告、无水平溢出、现有非聊天视图没有视觉/行为回归。

## 9. 测试与验收清单

### 自动检查

- `cd frontend && npm run typecheck`
- `cd frontend && npm run build`
- 若新建了纯展示组件测试，覆盖状态映射、按 run/plan 过滤、排序与空状态；避免只镜像实现细节的快照测试。
- 既有会话/SSE/审批相关测试按实际改动运行；本次如未改状态投影，不扩展后端测试范围。

### 手工场景

1. 空会话：创建会话、选择会话、加载中状态、首次发消息。
2. 多轮会话：普通文本、长 Markdown、代码块、表格、用户附件、tool 输出。
3. 运行中：thinking 到 running，进度事件追加，停止运行，网络/SSE 断连与恢复。
4. 并行计划：多个 task 的 queued/running/retry_wait/completed/failed/waiting_approval。
5. 审批：逐项选择、批量选择、提交、提交失败、SSE resolved 后继续执行。
6. 附件：选择、上传中、processing、ready、failed、删除、粘贴与拖放。
7. 切换 session：历史 REST 与 SSE replay 并发返回，旧 session 状态不得闪现。
8. 响应式：1440px 桌面三栏；1024px 右侧抽屉；390px 手机会话抽屉/执行抽屉/固定 Composer。
9. 键盘与辅助技术：Tab 顺序、Enter/Shift+Enter、Escape 关闭、抽屉 aria 状态、图标按钮标签。

### 验收门槛

- typecheck/build 通过。
- 每个可见按钮都已连接真实行为或不显示。
- 任何 run/task/approval/event 均来自现有数据，不存在固定示例内容。
- 任务/事件按当前 run 过滤，切换 session 不泄漏上个 session 内容。
- 桌面与手机均无输入区遮挡、横向溢出或不可关闭的覆盖层。
- 会话管理、消息发送、附件、审批和取消行为与改版前一致。

## 10. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| store 中任务/时间线可能包含当前 session 多个 run | 右栏展示旧请求数据 | 用消息 run_id、lifecycle timeline 和 plan.run_id 建立过滤；没有足够关联时只展示当前明确可关联部分 |
| `ActivityPanel` 既有请求分组语义与执行详情不同 | 重构后代码职责混杂 | 优先新建面板组件，保留已有 `ActivityPanel` 供原逻辑/其他调用点使用，确认引用后再移除 |
| 浅色静态稿与全局深色主题冲突 | 管理页出现意外变色 | 用聊天页根类 scoped tokens，不全局替换 athena 色板 |
| 移动端 11 个管理入口空间不足 | 导航挤压或难以触达 | 聊天侧栏改成分组文字导航；移动端通过独立导航抽屉承载所有入口 |
| 审批按钮表现成立即执行 | 高风险工具误执行 | 延续选择决策后再提交，审批动作不可变成单击即提交 |
| 全局 App 外壳改动影响管理页 | 非聊天回归 | chat/non-chat 独立布局分支，非 chat 分支只复用现有组件 |
| 工作树已有未提交代码 | 丢失用户工作 | 编辑前检查目标文件 diff，采用局部 patch，完成后核对 diff 与 status |
| 静态示例含未实现模型/提示词功能 | UI 暗示无法兑现 | 首期不展示对应按钮，另行确认功能 API 后再扩展 |

## 11. 实施时的变更控制

1. 开始代码实施前读取本方案及执行当时的 `git status`、目标文件完整 diff。
2. 先完成阶段 A 并检查布局，不同时改状态投影和后端协议。
3. 每阶段后运行适当验证并查看目标文件 diff，发现无关变化时保留，不做清理式回滚。
4. 共享样式改动优先限定在 `.chat-workspace` 根节点下；若改全局样式，必须列出受影响视图并回归检查。
5. 任何新增 API 字段或服务端假设都暂停该项 UI 展示，回到现有可靠数据，不用 mock 伪装已接通能力。

## 12. 完成定义

改版在真实 React 页面达到目标信息架构，所有既有聊天能力和审批语义保留，任务/事件来源可信，桌面/窄屏检查通过，typecheck/build 通过，且变更 diff 不包含静态演示数据或无关重构时，可视为本实施完成。
