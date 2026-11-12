# 工具调用实施方案（首期精简版）

状态：用户已确认并授权实施的首期精简稿；待独立 Review 与最终验收。本文不表示 Review PASS。
依据：docs/tool-execution-design.md 的核心流程；2026-11-09 按“先完成可靠主流程”精简。
代码核对日期：2026-11-09；基于 v2 当前工作树，包括未提交的 Run、LLM 与上下文压缩实现。

> **后续代码状态（2026-11-12）**：`adapter/llm/openai_responses.py` 已删除，当前只有 Chat Completions 适配器。下文涉及“两个适配器”或 Responses 校验的表述均为历史实施记录，不再代表当前代码。

## 1. 首期目标与边界

后端完成 MCP、memory_search、knowledge_search 的统一执行闭环：固定工具目录、模型单工具串行调用、执行前持久化、结果回传、多轮回答、取消、权限校验和持久事件。

首期包含：

- 每 Run 最多 40 次应用层 LLM 请求，包含正常生成、压缩和应用层模型重试，跨澄清累计；工具执行本身不计次，不另设工具调用次数上限。
- 使用 Run 的原子计数记录额度，不建立逐请求准入表。每次 provider 调用前先计数，失败或无 usage 也占一次。
- 单工具默认 30 秒，结果默认 64 KiB，执行段默认最多 300 秒，资源关闭最多额外 10 秒。
- MCP 每次工具调用独立建立连接，调用结束或失败后关闭；同一 Run 不持有跨调用会话。
- 工具执行层不自动重试；MCP 和内置工具都只在一次调用中执行一次。
- 业务工具产生 unknown 后，保存安全结果并直接失败 Run，不再调用模型或工具；用户通过查询了解情况，再主动 retry。
- 取消、超时或失租后，未完成调用收敛为 unknown；迟到结果不写入数据库。
- 有 unknown 时，用户通过一次明确确认创建新的 retry Run；新的 Run 从头执行，不自动复制或复用旧 Run 的工具结果。
- 所有 retry 均明确提示整个任务从头执行可能重复此前已成功的操作，不仅提示 unknown 风险；首期只提供后端说明和查询契约，不实现前端弹窗。

首期明确不实现以下扩展；它们不进入代码、测试和验收标准：

- 原 Worker 失租后的原子恢复和暂停/恢复执行门。
- deadline 或取消之后的迟到结果事实结算及清理租约。
- 跨 Run 的按需工具结果复用、定义/参数规范化和复用来源链。
- 同一 Run 内 MCP 会话或 stdio 进程复用。
- 多个 unknown 的细粒度 ID 确认、retry 链遍历、链深度和循环检测。

不包含前端页面、存量数据迁移/回填、新旧 Worker 混跑兼容、跨 Run 连接池、内置写工具、自动工具目录刷新、子 Agent 或多模态结果读取。不修改原 Athena 仓库。

## 2. 当前代码与实际差距

| 当前源码 | 已有能力 | 本次变化 |
| --- | --- | --- |
| app/tool/manager.py；domain/tool/model/entity.py、ports.py | 管理 MCP 目录、内部与公开投影；类型要求 MCP 来源 | 保留管理职责，新增通用运行目录和分派，不让内置工具伪装 MCP |
| domain/mcp/ports.py；adapter/mcp/repository/mcp.py | 发现、加密配置、运行配置读取 | 增加一次调用端口；每次调用读取一致的配置版本 |
| infrastructure/mcp/discovery.py、http.py、stdio.py | mcp==2.3.0、受控发现、网络与进程限制 | 实现真实单次调用、连接释放和结果规范化 |
| app/run/executor.py、context.py、prompt.py | 只接受 request_user_input；单轮生成 | 加入工具循环、额度计数和最终回答模式 |
| domain/message/model/part.py | 调用/结果块；结果只有 success/error | 复用协议，用业务结果包装 unknown，不扩张供应商角色 |
| domain/run/model/entities.py、ports.py；adapter/run/repository/run.py | Run 命令、执行租约、retry、消息上下文、usage | 增加工具快照、调用记录、原子 LLM 计数和取消收敛 |
| app/run/worker.py | heartbeat 失败后停止执行；失租回收终结 | 保持失租终结；不增加原 Worker 恢复 |
| app/run/compression.py；domain/run/model/stream.py | 压缩、SSE、文本增量 | 共用 LLM 计数，保持工具协议成对 |
| app/memory/service.py；app/knowledge/service.py；app/project/knowledge.py | 记忆召回、知识检索、项目关联 | 增加只读薄处理器；项目知识库最多 20 个 |
| api/dto/run.py；app/run/service.py | retry 只有 request_id | 增加 unknown 的整体确认和幂等校验 |

新增文件名在实施时可按既有导出约定微调，职责与分层不能随意替换。共享日期和 JSON 处理复用 backend/src/common/date.py、json.py；实现类显式声明端口，数据库 JSON 字段使用显式 Pydantic 类型。

## 3. 目标契约和持久化

### 3.1 运行工具类型

在 domain/tool/model 增加运行定义与结果类型，建议文件 runtime.py、execution.py：

- RuntimeToolDefinition：稳定 tool_id、model_name、description、input_schema，以及带判别字段的 builtin/mcp 来源。builtin 含版本；mcp 含 server_id、remote_name、config_version。
- ToolExecutionContext：服务端生成的 owner、聊天 Session、Run、执行代次、认证来源和截止时间；模型参数不能覆盖。
- ToolOutcome：status、稳定 code、安全 message、data、truncated。状态区分 ok、明确失败、cancelled、unknown；内置 disabled/degraded/unavailable 不能等同于零命中。

公开投影不包含内部来源配置。ToolResultPart.output 保存合法 JSON 的规范化结果；协议 success/error 与 ToolOutcome 分离，unknown 不转换为 success。不保存完整无界原始结果。

### 3.2 Run 与工具调用记录

Run 增加类型化工具快照、后台认证来源和原子 LLM 请求计数。建议字段：

- tool_snapshot：有容量上限的类型化 JSON，不放凭据、URL、command 或原始异常。
- auth_session_id 或等价认证来源：只保存内部引用和 owner 绑定，不保存 Cookie/token。
- llm_request_count：非负整数，默认 0，数据库检查 `0 <= count <= 40`。
- retry_of_run_id：用户 retry 的来源 Run。

建议 run_tool_calls：

| 字段 | 用途 |
| --- | --- |
| id、run_id、session_id | 调用归属；与 Run 建立同 Session 约束 |
| tool_call_id、ordinal | 模型协议 ID、Run 内记录顺序 |
| tool_id、tool_kind、model_name | tool_id/tool_kind 可空以记录未知工具名；model_name 必填且有界 |
| execution_epoch、status | 执行归属及 executing/completed/failed/cancelled/unknown |
| call_message_id、result_message_id | 指向唯一协议内容，参数和结果不重复存储 |
| started_at、finished_at、safe_error_code | 时间、耗时和安全错误 |

唯一约束为 `(run_id, tool_call_id)` 和 `(run_id, ordinal)`；完成结果关联唯一。来源被删除不得级联抹掉历史调用；消息引用必须与 Run/Session 同属。首期不增加 `reused_from_call_id`。

### 3.3 LLM 请求计数

不建立逐请求准入表，使用 `llm_request_count` 作为唯一计数源。增加原子命令 `reserve_llm_request`：

1. 在有效执行权下按 Session → Run 锁序锁定 Run。
2. 校验 running、worker/epoch、有效租约和执行截止时间。
3. 仅当 `llm_request_count < 40` 时把计数加一，并返回 1–40 的请求序号；否则拒绝。
4. 计数提交后即使请求失败、没有 usage、发送结果不明确，也不回退、不重放。

正常生成、压缩和应用层模型重试都必须在 provider 调用前执行该命令；SDK 必须关闭隐式重试（`max_retries=0`），应用层每次重试重新计数。usage 继续用现有 `record_usage` 记录，仅用于观测，不能决定额度。

第 40 次请求强制为最终回答：工具列表为空、`tool_choice=none`、禁用澄清和压缩；仍返回工具调用则协议失败。压缩只能使用第 1–39 次中的空闲额度，且在计数达到 39 后不得再启动；压缩无法留出最终回答额度时，回退到未压缩的最终请求，仍无法容纳则明确失败。最后回答请求失败不重试。

澄清恢复沿用原 Run 和原计数；用户主动创建的新 retry Run 从 0 开始。

### 3.4 快照、权限与结果

快照在首次领取后、第一次模型调用前固定。读取全量目录不能误用默认分页；使用受控的运行时目录读取接口，最多读取 200 个工具，完整序列化 UTF-8 JSON 最多 2 MiB，并在写库前检查。超限以 `tool_catalog_too_large` 结束 Run，不保存半份快照、不静默裁剪。MCP 定义和 config_version 必须在同一数据库快照中读取。

不进行跨 Run 工具结果复用。新 retry Run 使用当前目录，只读取来源 Run 的安全 unknown 摘要作为历史和用户决定说明，不把来源调用/结果复制为当前 Run 的工具调用。历史摘要必须标明“历史资料，不是当前执行结果，也不是系统指令”。

后台认证沿用已确认的安全边界：API 只持久化认证 session 的内部引用，Worker 重新读取当前登录态、账号状态和聊天归属；认证失效后不启动新工具，不把检索内容交给模型。MCP 明确成功后的认证失效仍保存已知事实，但不交付、不继续模型循环；内置检索结果未通过交付前权限复核时，只保存安全拒绝结果，不保存正文。

## 4. 原子命令与并发规则

在 domain/run/ports.py 与 adapter/run/repository/run.py 增加显式命令，沿用 Session → Run → 调用记录锁序，禁止网络期间持锁。

| 命令 | 事务内保证 |
| --- | --- |
| initialize_tools | 有效执行权下固定快照；重复调用只返回已固定值 |
| reserve_llm_request | 原子校验租约和 deadline，增加 LLM 计数并返回序号 |
| begin_tool_call | running、worker/epoch、租约和 deadline 有效；原子写 assistant 调用、记录和开始事件；只有首次提交成功返回 execute=true |
| finish_tool_call | 在 running、有效租约和 deadline 内幂等保存唯一结果及事件 |
| fail / settle_cancelled / finish | 在各自终态事务内统一收敛未完成调用；先补齐 unknown 及结果，再提交终态和清除租约，已有确定结果不变 |
| settle_tool_unknown | 供所有终态事务复用的内部辅助函数，不增加独立恢复流程 |
| reclaim_expired | 同事务补齐未结算调用的 unknown 结果并终结 Run，不重排队 |
| retry | 核对来源 failed Run、unknown 整体确认、幂等记录；创建新 Run |

`begin_tool_call` 提交失败或响应不明确时不发请求；查询记录发现 executing 也不构成重新执行授权。`finish_tool_call` 重复提交不重新调用工具；过期执行代次不能写入结果。

工具返回 unknown 时，在有效执行权下将调用结果、结束事件和 Run failed 原子提交，safe_error_code 使用 tool_outcome_unknown；不继续模型循环。与取消竞争时沿用已有状态优先规则，最终可为 cancelled，但都不得再次调用模型或工具。

所有 Run 终态事务必须复用同一个事务内收敛辅助函数，覆盖现有 fail（普通异常、认证失效、执行超时等）、取消结算、失租回收和正常 finish 的终态不变量：未结算 executing 调用补齐 unknown、安全 ToolResultPart 和结束事件，已有确定结果不变，终态后不得留下 executing 或缺失协议结果。不能先失败 Run/清空租约，再依赖回收器补齐。仅需改造既有事务，不新增清理租约、迟到结算或恢复服务。

取消、deadline 到期、失租或 Worker 停止后，不再保存迟到结果：先停止发起新调用，关闭当前外部资源，再由取消/回收事务把未完成调用收敛为 unknown。迟到结果即使外部已经成功，也不再覆盖本地 unknown；首期不提供额外的清理结算通道。

不限制工具次数；参数错误、不可用和未知工具都记录事实，后续实际 LLM 请求消耗统一额度。工具名称不存在时允许调用记录只保存模型名和安全拒绝原因，不强造不存在的 tool_id。ordinal 只表示工具顺序和分页，不代表模型额度。

### 4.1 租约与停止

Worker 心跳失败、租约过期或取消后立即停止发起新的模型请求和工具调用。原 Worker 不尝试恢复自己的租约，也不重新接管 Run；其他 Worker 只按 `reclaim_expired` 收敛失租 Run。清理仅关闭本次 MCP 连接、取消在途请求和回收进程，最多使用额外 10 秒，不执行业务，不延长 deadline。

## 5. MCP 与内置工具

### 5.1 MCP 单次调用

S0 对锁定 `mcp==2.3.0` 的源码、官方资料和可执行样例核对调用入口、取消、错误类型、参数 Header 和内部重发行为。通过标准：受控 HTTP 与 stdio 均真实执行一次；没有业务请求自动重发；协议/配置错误安全返回；异常和取消有界关闭；已有网络/凭据限制不失效。

首期每次工具调用建立一次连接，使用当前 Server 配置和 `config_version`，在该调用结束、失败、取消或超时后关闭。连接建立和协商计入单次工具 30 秒预算；不持有跨调用会话，也不跨 Run 共享。并发 MCP 调用不属于首期范围。

只转换文本和 structured JSON，不拉取 URI、图片、音频。响应读取上限沿用受控 transport，与规范化 64 KiB 分开；UTF-8/JSON 截断后必须合法，保留状态、安全 code 和 truncated。MCP 明确成功后清理失败仍保留成功结果，另记清理错误；请求可能已发出但没有确定结果时记 unknown。

### 5.2 内置工具

建议 app/tool/builtin/memory.py、knowledge.py、context.py；不新增内置工具数据库目录。

- memory_search：query 1–2000 字符，limit 默认 5/最大 8，recall 预算 1200 tokens。个人与项目范围由服务端映射；区分关闭、降级、不可用和空结果。
- knowledge_search：query 1–4000 字符，limit 默认 5/最大 8，建议 context_budget 2000 tokens。仅查询当前项目关联且当前用户仍有权访问的知识库；使用 ProjectKnowledgeService、KnowledgeService；结果带安全来源 ID 和片段，不含本地路径。
- 每项目最多绑定 20 个知识库。调整 ProjectKnowledgeService.bind 和关联仓储：同一事务先锁项目行，已关联直接幂等成功，未关联才检查当前关联数量并插入；第 21 个返回明确领域错误和安全 API 错误码。解除绑定后可释放名额。
- 参数 extra=forbid，拒绝布尔数量、空白 query、owner/base_ids 等越权参数；本地 JSON Schema 校验禁止网络 $ref。
- 查询前和结果交付前复核认证/聊天可见性；取消向下传播，不转成空结果，不自动重试。

## 6. 模型循环与上下文

修改 app/run/executor.py、context.py、prompt.py、compression.py：

1. 取得认证上下文、固定目录、原协议和 retry 来源的安全摘要，检查执行许可。
2. 计算包含完整 Schema、协议、结果和输出预留的预算；目录不静默裁剪。
3. 发出单轮模型请求，收完整响应后判定：无工具则最终回答；澄清则保存并等待；单业务工具则走原子调用；多个工具则协议失败且不执行任一个。
4. unknown 结果保存后直接失败 Run，不再请求模型或工具；其他合法结果提交后再次循环，每轮重新预算。压缩不拆调用/结果对，未结算调用不进入下一轮。
5. 每次 provider 请求前执行 `reserve_llm_request`；第 40 次强制纯回答，禁用工具、澄清和压缩。
6. waiting 与终态转换前关闭工具连接；状态和取消结算仍遵守既有规则。

校验职责：调整 adapter/llm/openai_chat_completions.py，将输出工具名称匹配和业务参数 Schema 校验移至工具执行层；适配器保留协议结构、非空合法调用 ID、参数可解析为 JSON 对象和大小校验。非法 JSON、非对象参数或缺失调用 ID 仍是协议错误；未知工具、工具不可用或 Schema 不匹配由执行层保存拒绝结果，再交回模型修正。

提示词版本：保留 `run-v1` 原文不变，新增 `run-v2` 工具循环模板；新提交和 retry 使用 v2。执行器按 Run 固定版本选择能力：v1 只支持原澄清模式，不因部署获得业务工具；v2 使用本方案工具循环。

Chat Completions 必须映射 `request.tool_choice`，处理 `auto`、`none`、`required` 和指定工具；最终纯回答请求明确传 `none`。适配器测试必须验证发往 SDK 的实际参数，不能仅断言领域请求字段。

现有模型临时错误重试只针对无输出请求，不能包裹工具执行。每次模型循环使用唯一 operation_id，usage 中区分轮次和 attempt；任何重试重新执行 `reserve_llm_request`。不实现跨 Run 自动结果复用；retry 上下文只带安全 unknown 摘要。

文本增量增加模型轮次或 operation_id，发布、Redis、SSE 和测试同时更新；工具前说明保存到 Execution，不作为最终答案。正式消息只在既有原子命令中提交。

## 7. 用户 retry、查询与事件

### 7.1 unknown 决定

扩展现有 RunRetryRequest，增加布尔字段 `confirm_unknown`（默认 false）。首期不支持逐调用 ID 或部分确认：

- 来源 Run 存在未解决的 unknown 且 `confirm_unknown=false` 时，返回 409 `tool_retry_confirmation_required`，不创建新 Run。
- 用户先通过工具调用查询了解 unknown；提交 `confirm_unknown=true` 后创建新的 retry Run。
- retry 只使用最新 failed Run、当前目录和新 Run 的 40 次额度；不遍历 retry 链，不支持部分 unknown，不自动重放旧工具调用。
- 新 Run 的上下文只包含来源 unknown 的安全摘要和用户确认说明，不包含来源工具结果正文。旧 unknown 记录不修改。

所有 retry 的后端接口说明和查询投影均提供明确提示：“整个任务将从头执行，可能重复此前已成功的操作；原操作结果未知时也可能已生效。”没有 unknown 也可能重复成功的写操作，不承诺只重做失败步骤。confirm_unknown 仍仅用于来源存在 unknown 的整体确认，不新增成功结果复用或普通工具审批。

具体确认字段是后端交互契约，不实现前端确认弹窗。

### 7.2 查询与事件

在 api/runs.py、api/dto/run.py、api/converter/run.py 与 app/run/service.py 增加：

- GET /api/v1/sessions/{session_id}/runs/{run_id}/tool-calls：按 ordinal 游标分页。
- GET /api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/{call_id}：返回参数、规范化结果、状态、起止/耗时、截断信息。

列表返回摘要，详情最多有界参数/结果；内部 server_id、remote_name、URL、凭据和 SDK 异常不进入公开 DTO。查询核验 owner、聊天未删除和真实认证；不存在/不可见不泄露差异。参数上限 64 KiB；超长参数直接协议失败，不执行工具，不创建占位调用。

持久化空值约定：executing 的 result_message_id 可空；已正常结算（含错误、unknown、权限拒绝）的调用必须有本 Run 的结果消息。内置检索交付前权限失效时，调用记 failed，结果只保存安全拒绝包装，不保存正文；所有调用/结果成对，失租回收补齐 unknown。

新增 tool_call_started、tool_call_finished 持久 RunEvent，payload 仅含调用 ID、ordinal、安全状态和轮次。结果较大时不塞入 SSE；客户端通过详情查询读取。复用现有 Pub/Sub 与数据库对账。

## 8. 实施阶段与交付门槛

严格遵循 Root → Luna Implementer → Terra Reviewer → Root。用户确认本文后才启动代码实施；Reviewer 只审实际 Diff，不改代码，修复最多三轮。

| 阶段 | 工作与主要文件 | 阶段完成条件 |
| --- | --- | --- |
| S0 依赖核验 | 锁定 SDK、infrastructure/mcp、受控服务测试 | HTTP/stdio 单次调用、取消、清理和不重发有证据 |
| S1 契约与持久化 | domain/tool、domain/run、adapter/run 模型/仓储；建表定义 | 快照、LLM 计数、调用结果、回收和认证来源可原子落库；PG 竞争测试通过 |
| S2 工具执行 | app/tool、单次 MCP 调用、两个 builtin | 权限、参数、版本、结果规范化和连接关闭通过 |
| S3 Run 闭环 | app/run、Worker、LLM usage、stream | 串行多轮、澄清、40 次计数、取消和协议成对通过 |
| S4 API 与组装 | api/runs、DTO、main.py、run_worker.py、README、.env.example | retry 确认、分页查询、事件对账和真实依赖生命周期可运行 |
| S5 整体验收 | 全部受影响代码及集成测试 | Review PASS、必要检查通过、未验证环境明确 |

S1 先冻结端口形状，S2 再实现；不得用测试替身代替阶段完成的真实执行路径。S0 可通过临时脚本验证，不把探针固化为生产功能。

## 9. 验证矩阵

| 场景 | 必须观察到的结果 | 验证方式 |
| --- | --- | --- |
| 多工具循环 | MCP → 内置查询 → 最终回答；每轮 usage 不丢失 | 确定性 provider + 真实 Run/消息仓储 |
| LLM 额度 | 正常生成、压缩和应用层重试共用 40 次；第 40 次只回答；并发争抢只有一个请求获得额度；SDK 无隐式重试 | PG 原子竞争 + provider 请求次数断言 |
| 工具调用幂等 | begin 重复只有一次执行；finish 重复只有一条结果 | 同步屏障竞争测试 |
| 超时/取消 | 未完成调用 unknown；停止后迟到结果不落库；不重放 | 可控返回时刻 + PG |
| unknown 停止 | 返回 unknown 后调用结果与 Run failed 原子保存，不再发模型/工具请求；取消竞争仍停止 | 执行器请求次数断言 + PG/API |
| 终态收敛 | 普通异常、认证失败、执行超时、取消和回收终结均无 executing 遗留且调用/结果成对，已有确定结果不变 | 现有 fail/取消/回收/finish 仓储集成测试 |
| 失租回收 | reclaim 补齐 unknown 并终结 Run；原 Worker 不恢复、不重写终态 | PG 双连接 + Worker 测试 |
| MCP 单次调用 | 同一工具每次独立建立并关闭连接；HTTP/stdio 不自动重发；配置版本变更拒绝旧调用 | 受控 MCP 服务 |
| 内置权限 | 关闭、降级、无项目、无关联库和跨用户范围均安全；项目第 21 个知识库拒绝，满额重复绑定幂等 | 服务层 + 真实 PG/API |
| unknown 确认 | 未确认 409；确认后新 Run 从 0 计数；旧 unknown 不改，不自动复用旧结果 | 认证 API + PG + Worker |
| 从头 retry 说明 | 包含与不包含 unknown 的来源均提示可能重复已成功操作，不承诺只执行失败步骤 | 查询 DTO + API 契约测试 |
| 结果边界 | 文本/JSON 合法有界；非文本不下载；参数超 64 KiB 协议失败；结果截断标记正确 | transport + 规范化测试 |
| 快照容量 | 目录最多 200 个、完整快照最多 2 MiB；超限写库前失败且无半快照 | 有界读取 + 仓储测试 |
| 适配器校验 | Chat Completions 对未知工具/Schema 不匹配交给执行层；非法 JSON、非对象参数、缺失调用 ID 仍协议失败；最终请求实际传 none | 适配器 + 执行器测试 |
| 事件对账 | Pub/Sub 丢失仍可查询，重连可重建，旧代 delta 被过滤 | Redis + PG/SSE |

建议新增 test_tool_runtime.py、test_builtin_tools.py、test_mcp_execution.py、integration/test_run_tools_postgres.py、integration/test_tool_execution_e2e_postgres.py；扩展现有 Worker、compression、stream、runs API 和 MCP transport 测试。

先记录当前工作树基线，再跑受影响测试；阶段结束执行项目全量 pytest、Ruff check、修改文件 format --check、mypy 和 diff whitespace 检查。真实数据库测试使用隔离测试数据库；无环境时明确报告跳过，不能用 SQLite 证明 PG 行锁正确。

最终至少验证受控真实 HTTP 与 stdio MCP 服务。模型多轮可用确定性 provider 验证运行机制；未进行真实供应商联调时单独标明。

## 10. 运行配置与最终交付

在现有 Run Worker 配置上增加：LLM 请求数 40、工具超时 30 秒、规范化结果 65536 字节、有界参数 65536 字节、执行段 300 秒、资源关闭最多 10 秒。配置启动时校验为正且有明确上限；工具 deadline 取部署值与当前执行段剩余时间的较小值。

每次从 queued 领取进入 running 获得最多 300 秒执行预算；进入 waiting 时结束本段计时，等待不消耗预算，回答后重新领取获得新预算。同执行段内工具调用不重置 deadline；40 次 LLM 请求上限跨澄清累计。等待时间不消耗额度，新 retry Run 从 0 开始。

Worker 与 API 共用依赖构造函数，按已启用配置装配记忆/知识查询模型；避免直接导入 API app 获取 app.state。模型按进程共享，MCP 连接按单次工具调用持有并关闭。一项关闭异常不能阻止其他关闭，不输出密钥或远端原始异常。

交付时更新 README、.env.example 和两份工具文档，记录启动方式、依赖模型/数据库/Redis、权限范围、unknown 和手动 retry 契约。数据库目标结构按开发环境初始化即可，不增加生产迁移操作清单。

Root 最终报告修改文件、实际验证结果、Reviewer 结论及未验证项；不自动提交、推送或部署。

## 11. 后续优化（不属于首期）

以下能力只在首期闭环稳定后重新评估，不能作为首期验收项：

- 逐请求 LLM 准入审计和精确恢复。
- 原 Worker 失租恢复。
- deadline/取消后的迟到结果结算。
- 跨 Run 工具结果自动复用。
- 同一 Run 内 MCP 会话/进程复用。
- 多个 unknown 的细粒度确认和 retry 链。
- 更复杂的快照分页和容量流式处理。

若重新引入上述能力，需要单独更新设计、补充并发测试并重新获得用户确认；实现者不得在首期静默加入。
