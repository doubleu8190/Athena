# 工具调用审批与并发批次设计

状态：核心产品规则已确认，方案已实施并通过审查。
核对日期：2026-11-09。

本文扩展 [工具调用设计](tool-execution-design.md) 和 [Run 设计](run-design.md)。它只替代其中“管理员配置工具一律无需审批”和“单轮最多一个业务工具”的限制；其他消息、工具结果、租约、取消、失败和清理规则继续有效。

## 1. 目标与范围

管理员可以显式标记某些工具需要运行期审批。用户审批后，同一个 Run 从持久化断点继续执行，不重新调用此前的模型请求，也不重放已经完成的工具调用。

同一模型响应返回多个工具调用时，它们形成一个工具批次。首期支持受控的并发执行，但审批决策、结果交付和失败收敛保持批次一致。

### 1.1 已确认规则

- 审批是同一个 Run 的持久化等待点，不是创建新 Run，也不是从头重试。
- 工具通过管理员配置的 `requires_approval` 决定是否审批，默认关闭。
- 审批策略随 Run 的工具快照固定；运行期间修改只影响后续 Run。
- 同一轮调用形成一个批次。任一调用需要审批时，整个批次暂停，不执行其中任何业务工具。
- 用户逐调用批准或拒绝；全部待审批调用都有决定后，Run 才重新排队。
- 拒绝只影响当前调用。被拒调用保存失败结果，其他已批准或免审批调用继续执行。
- 明确业务失败不扩散到其他调用；`unknown` 停止继续派发，让已经开始的调用收敛后失败整个 Run。
- 审批首期不自动过期，只能由批准、拒绝或取消结束等待。
- 单批最多 8 个调用，同一 Run 最多 4 个实际并发调用；超限整批协议失败。
- 审批等待和批准后排队可跨进程恢复；Worker 领取执行后失联则不自动续跑，安全失败。

### 1.2 首期不包含

- 管理员、审批人分离的多级审批流。
- “永久允许此工具”或跨 Run 的审批记忆。
- 审批自动过期、后台催办和通知系统。
- Worker 领取执行后的自动崩溃恢复或跨 Run 结果复用。
- 外部工具之间的分布式事务、回滚或恰好一次执行保证。
- 为审批新增独立服务、消息队列或通用工作流引擎。

## 2. 当前实现与差距

以下事实来自当前工作树源码核对。当前分支为 `v2`，工作树存在大量未提交的 Run、LLM、上下文压缩和工具执行改动；实施时必须保留这些工作。

| 已核对位置 | 当前行为 | 所需变化 |
| --- | --- | --- |
| `backend/src/domain/run/model/entities.py` | Run 只有 `waiting_for_input`；输入请求绑定会话问题 | 增加审批等待状态、批次事件和工具调用审批字段 |
| `backend/src/app/run/executor.py` | 单次模型响应只接受一个工具；工具调用立即执行 | 接受受控多调用批次；审批时暂停；恢复时先执行已批准批次 |
| `backend/src/adapter/llm/openai_chat_completions.py` | 请求固定发送 `parallel_tool_calls=False` | 按模型能力开启并行调用，并验证多调用流式解析 |
| `backend/src/domain/llm/model/profile.py` | 模型能力只有 stream 和 tools | 增加并行工具调用能力声明，避免强制不兼容供应商开启 |
| `backend/src/domain/llm/model/types.py` | 响应和消息结构理论上支持多个 `LlmToolCall` | 可复用协议类型，不需要另造工具消息格式 |
| `backend/src/app/run/context.py` | 能把一个 assistant 消息中的多个 `ToolCallPart` 转为 `tool_calls` | 批次结果必须按调用序号稳定提供给模型 |
| `backend/src/adapter/run/repository/run.py` | `begin_tool_call` 强制 assistant 消息恰好一个调用；调用立即进入 `executing` | 增加原子批次提交、逐调用审批和恢复查询 |
| `backend/src/adapter/run/repository/run.py` | `finish_tool_call` 遇到 `unknown` 立即失败 Run，并把其他 `executing` 调用收敛为 unknown | 批次执行时需要先停止派发并等待已开始调用，再统一失败 |
| `backend/src/adapter/run/model/tool_call.py` | 状态只有 executing/completed/failed/cancelled/unknown；调用消息唯一 | 增加等待审批、已批准状态；允许一个 assistant 消息关联多个调用 |
| `backend/src/domain/tool/model/runtime.py` | 运行工具定义没有审批策略 | 增加类型化的 `requires_approval` 并纳入工具快照 |
| `backend/src/adapter/mcp/model/mcp.py`、`backend/src/adapter/mcp/repository/mcp.py` | MCP 工具没有审批配置 | 保存管理员策略，并在重新发现工具时按 remote name 保留 |
| `backend/src/api/runs.py`、`backend/src/api/dto/run.py` | Run 详情只有 `pending_input` | 增加待审批调用投影和决策端点 |
| `backend/src/app/run/stream.py` | SSE 只在 `waiting_for_input` 和终态停止 | 审批等待也停止当前流，并通过快照/查询恢复 |
| `backend/migrations/versions/20261111_0012_create_runs.py` | Run 状态、活跃 Run 唯一索引和命令操作不含审批 | 新迁移扩展状态约束、活跃索引和命令类型 |
| `backend/migrations/versions/20261113_0014_add_tool_execution.py` | 调用状态和唯一调用消息约束不允许批次与审批 | 扩展状态约束、字段和索引 |

当前最关键的缺口不是“暂停执行”，而是恢复入口。现有 Worker 每次领取后直接从模型请求开始；如果只把 Run 标成等待审批，批准后重新领取会再次请求模型，无法保证从原工具调用继续。因此必须持久化工具批次，并在任何模型请求之前恢复未完成的已批准调用。

## 3. 核心流程

### 3.1 术语

- **工具批次**：同一个 assistant 响应中的全部工具调用。它们共享一个 assistant Execution 消息和模型轮次。
- **审批项**：批次中的一个工具调用。审批以 `run_id + tool_call_id` 为作用域，不跨调用复用。
- **批次屏障**：只要批次中存在待审批调用，批次内没有任何调用进入外部执行，包括免审批调用。
- **恢复断点**：已落盘但尚未结算的批次。Worker 重新领取后先结算该批次，再允许新的模型请求。

### 3.2 模型返回工具批次

1. LLM Adapter 校验完整响应、调用 ID、参数 JSON、调用数量和协议角色。
2. `request_user_input` 仍必须独占一轮。与任何业务工具同时出现时整批协议失败，不执行任何调用。
3. 模型能力未开启并行调用而返回多个调用时，整批协议失败。
4. 超过单批 8 个调用时，整批协议失败。
5. 每个调用的名称、参数对象和本地 Schema 在进入审批前校验。未知工具或参数不合法不请求审批，直接生成安全错误结果。
6. 对合法调用按冻结的工具快照读取 `requires_approval`。

### 3.3 原子提交批次

在一个事务中完成：

- 保存一个 assistant Execution 消息，其中包含按模型响应顺序排列的全部 `ToolCallPart`。
- 为每个调用建立一条 `run_tool_calls` 记录，序号连续且与消息中的调用顺序一致。
- 免审批调用初始为 `approved`；需要审批的调用初始为 `pending_approval`。
- 已确定参数无效或工具不存在的调用直接保存失败结果和 `failed` 状态，不进入审批。

如果存在至少一个 `pending_approval`：

- Run 从 `running` 转为 `waiting_for_approval`。
- 清除 Worker 租约和执行 deadline。
- 批次内任何调用都不发外部请求。
- 提交状态、审批信息和等待事件后结束当前 Worker 执行。

如果没有待审批调用，Run 保持 `running`，直接进入批次执行。

### 3.4 用户审批

- 用户只能审批当前 Run 中处于 `pending_approval` 的调用。
- 批准将调用改为 `approved`，不立即执行。
- 拒绝将调用改为 `failed`，保存稳定的 `tool_approval_denied` 结果。
- 部分审批完成时，Run 继续等待，不重新排队。
- 最后一个待审批调用被处理后，Run 原子地转为 `queued`，并刷新请求审批时使用的登录会话。
- 批准和拒绝绑定持久化的调用参数。客户端不需要也不能重新提交工具参数。

审批界面通过 Run 状态、Execution 调用记录和事件呈现，不新增用户 Conversation 消息。审批不是模型对话的一部分，也不应伪装成新的用户指令。

### 3.5 批准后恢复

Worker 重新领取同一个 Run 后：

1. 重新校验登录、Session、项目可见性、工具快照和执行权。
2. 在任何 LLM 请求之前查找当前批次中未结算的调用。
3. 已拒绝调用已经有结果，不再处理。
4. 已批准调用按稳定序号并发执行；达到并发上限的调用在批次内排队。
5. 全部调用结算后，才构造完整的一批 Tool 结果并与原 assistant `tool_calls` 配对，发起下一次模型请求。

因此恢复不会重放以下内容：

- 已经完成的模型请求；
- 已经成功或失败的业务工具调用；
- 已经写入的拒绝结果；
- 此前批次已经交给模型的工具结果。

### 3.6 批次结果与下一轮

- 同一批次的结果完成顺序可以不同，持久化时间也可以不同。
- 提供给模型时，assistant 调用消息之后必须紧跟该批次全部 Tool 结果，并允许按调用序号排列。
- 不允许只把部分结果交给模型，也不允许在未结算完的批次后插入下一轮 assistant 调用。
- 明确失败、拒绝和截断结果也是合法批次结果，模型可以看到并决定下一步。
- 只有整个批次没有 `unknown` 时，才允许发起下一轮模型请求。

## 4. 状态、数据与事务建议

本节具体字段、表名和事件名为实施建议，需随整体方案确认。

### 4.1 Run 状态

新增：

```text
WAITING_FOR_APPROVAL = "waiting_for_approval"
```

状态转换：

| 原状态 | 目标状态 | 条件 |
| --- | --- | --- |
| running | waiting_for_approval | 批次原子保存且仍有待审批调用 |
| waiting_for_approval | waiting_for_approval | 仅部分调用完成批准/拒绝 |
| waiting_for_approval | queued | 最后一个待审批调用完成决定 |
| waiting_for_approval | cancelled | 用户取消 |
| queued | running | Worker 领取并取得新执行代次 |

`waiting_for_approval` 必须加入：

- `runs.status` 检查约束；
- 每 Session 仅一个活跃 Run 的部分唯一索引；
- 取消、Session 删除保护和 SSE 停止条件；
- Run 详情与列表的状态投影。

等待不占 Worker 并发，但继续占用 Session 的执行位置。批准并重新排队后获得新的执行段预算，LLM 请求总次数继续累计。

### 4.2 工具调用状态

建议扩展为：

```text
pending_approval
approved
executing
completed
failed
cancelled
unknown
```

- 免审批调用的初始状态为 `approved`。
- 待审批调用的初始状态为 `pending_approval`。
- 批准后为 `approved`。
- 拒绝后为 `failed`，`safe_error_code=tool_approval_denied`，并保存对应 ToolResultPart。
- 只有原子取得执行准入后才能从 `approved` 转为 `executing`。
- 终态仍为 `completed`、`failed`、`cancelled`、`unknown`。

### 4.3 `run_tool_calls`

建议增加：

| 字段 | 用途 |
| --- | --- |
| `requires_approval` | 保存快照时该调用的审批策略 |
| `requested_at` | 批次被接受、审批被创建的时间 |
| `approval_decided_at` | 用户批准或拒绝的时间 |

同时调整：

- `started_at` 只表示真正开始调用外部工具的单调时间；待审批阶段为空。等待时长不能冒充执行时长。
- 一个 assistant `call_message_id` 可以关联多条调用记录，因此移除“每条调用独占 assistant 消息”的唯一约束，改为普通查询索引。
- `(run_id, tool_call_id)` 和 `(run_id, ordinal)` 唯一约束继续保留。
- `execution_epoch` 在实际执行准入时更新为当前代次，防止旧 Worker 写入结果。

不新增独立审批表。每个调用已经天然只有一次审批，调用记录本身保存审批状态和决定时间；审批命令幂等使用现有 `run_requests` 账本。这样避免审批表与调用表出现两个状态源。

`RunRequestOperation` 增加 `DECIDE_TOOL_APPROVAL`，对应迁移同步扩展 `ck_run_requests_op`。规范化指纹至少包含 Session、Run、调用记录和批准/拒绝决定；同键异内容必须冲突。

### 4.4 工具快照与管理员配置

`RuntimeToolDefinition` 增加：

```text
requires_approval: bool = False
```

`mcp_tools` 增加同名持久化字段，默认 `false`。内置工具在代码中显式声明，当前 `memory_search` 和 `knowledge_search` 均为 `false`。

MCP 重新发现并替换工具时，按 `remote_name` 保留已有审批策略；新增工具默认关闭。管理员通过独立的管理员工具策略接口修改，不复用普通用户工具接口。

Run 首次建立工具快照时固定此字段。审批等待恢复不重新读取审批策略；管理员修改只影响后续 Run。执行时仍按现有规则复核 MCP 配置版本和工具可用性，审批不能绕过删除、停用或权限撤销。

### 4.5 模型并行能力

首期 Run 只使用 Chat Completions 协议。建议为模型版本增加：

```text
supports_parallel_tool_calls: bool = False
```

- 为 `false` 时继续发送 `parallel_tool_calls=false`，响应中出现多个调用仍按协议失败。
- 为 `true` 时发送 `parallel_tool_calls=true`，接受 1 至 8 个调用。
- 无论供应商参数如何，平台仍以本地 8 个上限和批次校验为准。

不通过“先请求再失败重试”探测能力，避免重复模型请求和不确定费用。

### 4.6 配置上限

建议 `RunWorkerConfig` 增加：

| 环境变量 | 默认值 | 边界 |
| --- | --- | --- |
| `ATHENA_RUN_TOOL_BATCH_LIMIT` | 8 | 1–8 |
| `ATHENA_RUN_TOOL_CONCURRENCY` | 4 | 1–8，且不超过批次上限 |

普通用户和模型不能提高。单工具 30 秒和 Run 总执行 deadline 仍按现有规则计算；并发调用不会合并或延长单个调用的时限。

## 5. 批次执行与失败收敛

### 5.1 执行准入

- 每个调用在实际发出外部请求前，独立以当前 Worker、`execution_epoch`、租约和 deadline 原子从 `approved` 转为 `executing`。
- 未取得执行准入的调用不得发出请求。
- 参数和执行上下文从持久化的 assistant 调用消息读取，不接受审批请求携带的参数覆盖。
- 同一 Run 最多 4 个调用同时处于 `executing`，其余已批准调用按序号等待。
- 每次工具调用独立建立和关闭 MCP 连接，不引入跨调用会话复用。

### 5.2 明确失败

明确业务失败只保存该调用的 `failed` 结果：

- 不停止批次中其他调用；
- 不自动重试；
- 全部调用结算后，把成功、失败和拒绝结果一起交回模型。

### 5.3 `unknown`

`unknown` 包含工具超时、请求可能发出后的断流、无法确认外部效果等：

1. 立即停止从批次队列派发新调用。
2. 已经 `executing` 的调用继续在各自 deadline 内收敛并保存结果。
3. 尚未派发的 `approved` 调用不再执行，收敛为 `cancelled`，结果说明未执行。
4. 全部已开始调用结算后，Run 以 `tool_outcome_unknown` 失败，不再发起模型请求。
5. 已确定的成功或失败结果保留，不改成 unknown。

当前 `finish_tool_call` 发现单个 unknown 就立即失败 Run 并收敛其他执行中调用，不能直接满足上述“先等待同批调用收敛”的顺序。实施时需要把“保存单调用结果”和“批次终态收敛”分开，或增加显式的批次结算命令。

执行级致命错误，例如交付前登录失效或 Run 被撤销执行权，应同样停止派发新调用；已开始调用按现有安全清理和结果保留规则收敛，不能被伪装为业务失败。

### 5.4 取消与终态清理

- `waiting_for_approval` 取消时直接进入 `cancelled`，关闭全部待审批项，不创建新 Worker 执行。
- 尚未执行的 `pending_approval`、`approved` 调用收敛为 `cancelled`，并保存安全的“未执行”结果，便于 UI 和协议记录完整。
- `executing` 调用按现有规则尝试停止；无法确认结果的收敛为 `unknown`。
- 后续到达的批准或拒绝请求返回冲突，不能恢复已取消 Run。
- 删除 Session 继续被活跃 Run 阻止。

### 5.5 进程故障

- 等待审批时进程退出，不改变 Run 状态，审批完成后仍可重新领取。
- 批准后、Worker 重新领取前退出，Run 保持 `queued`。
- Worker 领取后、外部请求发出前失租，不自动续跑该 Run；未派发调用收敛为 `cancelled`，其他执行中调用按 unknown 处理。
- Worker 发出请求后失联，不自动重发。系统只承诺每个 Run 的业务调用最多发出一次，不承诺跨进程恰好一次。

## 6. 模型上下文与结果顺序

多个并发调用完成后，数据库可能按完成时间写入多个 Tool Execution 消息。模型上下文必须恢复为确定的批次结构：

```text
assistant: tool_calls = [call-1, call-2, call-3]
tool: result-1
tool: result-2
tool: result-3
```

建议在 `list_context_messages` 或相邻转换边界按 `run_tool_calls.ordinal` 对同一 `call_message_id` 的结果进行稳定排序，而不是依赖结果消息的提交顺序。

- 不得丢失任何已结算调用。
- 不得为同一个 `tool_call_id` 生成多条结果。
- 不得把其他批次的 assistant 消息插入未完成批次中间。
- 压缩和 Token 预算计算看到的是同一稳定结构。

## 7. API、事件与展示建议

### 7.1 Run 查询

现有 Run 详情增加安全的待审批投影：

```text
pending_approvals: [
  {
    call_id,
    batch_message_id,
    ordinal,
    model_name,
    tool_kind,
    requested_at
  }
]
```

完整参数继续通过现有 `GET /runs/{run_id}/tool-calls/{call_id}` 读取，并沿用 owner、Session 和登录校验。列表响应不一次返回最多 8 个、每个最多 64 KiB 的完整参数，避免详情和 SSE 快照膨胀。

### 7.2 审批端点

建议：

```text
POST /api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/{call_id}/approval
```

严格请求体：

```json
{
  "request_id": "client-idempotency-key",
  "decision": "approve"
}
```

`decision` 只允许 `approve` 或 `reject`。服务端：

- 校验当前登录用户、Session 可见性、Run 和调用归属；
- 只接受 `waiting_for_approval` 中的 `pending_approval` 调用；
- 同一幂等键同内容返回当前 Run，同键不同内容返回 409；
- 已决定为同一结果时返回当前状态，相反决定返回 409；
- 在最后一个决定事务中把 Run 改为 `queued`；
- 不使用 Conversation message 或 `run_input_requests` 承载审批。

不要求首期增加服务端“全部批准”复合命令。前端可用多个独立幂等请求实现一键批准；部分请求失败时剩余调用继续显示为待审批，语义清晰。

### 7.3 管理员策略接口

建议增加管理员工具策略更新接口，例如：

```text
PATCH /api/v1/admin/tools/{tool_id}
```

请求只包含 `requires_approval`。接口沿用管理员登录和 Origin 校验，只允许修改已注册 MCP 工具。工具不存在、已删除或请求字段扩展时返回明确错误；策略更新不复制连接配置和秘密。

### 7.4 typed Run 事件

建议增加：

- `waiting_for_approval`：批次进入等待，含批次消息 ID 和待审批调用 ID；
- `tool_approval_decided`：逐调用批准或拒绝；
- `tool_call_started`：只在实际取得执行准入后发出；
- `tool_call_finished`：逐调用保存终态；
- `approval_resumed`：最后一个决定完成后 Run 重新排队。

事件载荷只包含调用 ID、序号、状态和安全结果码，不包含凭据、Server 地址或未授权参数正文。SSE 在 `waiting_for_approval` 时结束当前流；客户端通过 Run 详情或重新订阅读取最新状态。

## 8. 安全与一致性边界

- 只有 Run owner 可以查看和审批自己的调用。
- 审批使用当前有效登录会话，并刷新 Run 的 `auth_session_id`；审批不会延长已撤销身份。
- 批准只授权当前调用的持久化参数，不授权同名工具的后续调用。
- 工具策略、可见性和参数 Schema 均来自冻结快照；执行前仍复核当前 MCP 配置和权限。
- 管理员停用或删除工具后，即使此前已批准，也不得发出旧调用。
- 审批等待期间的会话仍占用 Session 活跃 Run 唯一位置，不能在同 Session 启动新任务。
- 审批记录和工具调用记录都不能绕过现有 owner 隔离、软删除和日志脱敏。
- 外部效果可能部分成功，系统不提供跨工具回滚；UI 必须能表达成功、失败、拒绝、取消和未知。

## 9. 实施落点

保持 `api → app → domain / adapter` 结构。

- `domain/tool`：运行定义增加审批策略；批次命令和状态。
- `domain/run`：审批等待状态、事件、幂等操作和仓储命令。
- `domain/llm`：模型版本增加并行调用能力。
- `app/tool`：冻结策略、管理员策略更新和目录投影。
- `app/run`：批次判定、审批恢复、并发调度和失败收敛。
- `adapter/run`：批次原子写入、逐调用决策、恢复查询和终态清理。
- `adapter/mcp`：保存并保留工具审批策略。
- `adapter/llm`：按能力发送 `parallel_tool_calls`，验证多调用流式响应。
- `api`：Run 待审批投影、审批端点、管理员策略端点、事件 DTO 和 SSE。
- `backend/migrations/versions`：在实施时读取最新 Alembic head，新增迁移，不改写其他未提交功能。

日期和 JSON 复用 `backend/src/common/date.py`、`backend/src/common/json.py`。审批与批次状态使用明确领域类型，不以散装 dict 穿透应用边界。

## 10. 分阶段实施建议

### S1：协议、容量和迁移

- 增加模型并行能力、批次和并发配置。
- 扩展 Run 与工具调用状态、约束和索引。
- 增加 MCP 审批策略持久化和快照字段。
- 验证旧状态、旧调用记录和活跃 Run 唯一索引可升级。

### S2：批次持久化与审批 API

- 实现一个 assistant 消息对多个调用的原子提交。
- 保存免审批、待审批、拒绝结果和等待状态。
- 完成 Run 详情、逐调用审批、幂等和并发决定。
- 验证最后一个决定只排队一次。

### S3：恢复与并发执行

- 在 LLM 请求前优先恢复已批准批次。
- 实现最多 4 个调用的受控并发和序号稳定结果。
- 实现 `unknown` 停止派发及批次结算。
- 验证审批后不重复模型请求、不重放已完成调用。

### S4：流、文档与端到端验证

- 扩展 SSE 事件、快照和停止条件。
- 验证断线重订、等待期间重启、取消和迟到审批。
- 更新工具执行、Run 和 README 的配置说明。
- 完成 PostgreSQL、API、Worker、Redis/SSE 联合测试。

## 11. 验收标准

- 模型返回两个以上调用时，只有对应模型能力开启才接受；超过 8 个整批失败且无外部调用。
- 批次中一个工具需要审批时，全部调用暂停；免审批调用不得提前产生外部效果。
- 用户逐调用批准或拒绝；部分决定不会重新排队，最后一个决定只排队一次。
- 审批后 Worker 从原批次继续，不重新请求此前模型，不重复调用已完成工具。
- 被拒绝调用返回稳定错误结果，其他调用继续；全部拒绝仍允许模型生成最终解释。
- 明确业务失败不阻止同批其他调用；unknown 停止新派发，等待已开始调用结算后失败 Run。
- 实际并发不超过 4，排队调用按稳定序号派发，结果按调用序号进入下一轮模型上下文。
- 审批等待不消耗 Worker 并发和执行 deadline；取消可在等待期间立即结束。
- 旧 Worker、重复审批、相反决定、迟到审批和已取消 Run 均不能恢复或写入结果。
- 等待审批和批准后排队可跨进程恢复；领取执行后失联不自动续跑。
- 普通用户无法修改审批策略；策略随 Run 快照固定，管理员中途修改不影响当前 Run。
- 审批和结果记录不泄露凭据、MCP 地址、内部异常或未授权用户数据。
- 新迁移可从当前工作树 head 升级，保留既有 Run、消息、调用和工具配置。

## 12. 准备程度

核心用户流程、批次语义、审批策略、拒绝行为、失败耦合、等待期限、并发上限和进程故障边界已经确认，可以进入整体设计审查。

表字段、状态名、事件名、端点路径、模型并行能力开关和迁移方式是实施建议；它们没有改变已确认的产品行为，但在实施前应整体确认。

本文不授权修改业务代码。整体方案确认后，按仓库 `AGENTS.md` 执行 Root → Implementer → Reviewer → Root 流程；测试通过和本文自查均不替代独立 Review。
