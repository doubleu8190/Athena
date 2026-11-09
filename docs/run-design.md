# Run 首期设计

状态：设计方案已获用户整体确认；实施方案见 `run-implementation-plan.md`，S1–S6 已完成实施、独立审查及 Root 验收。真实供应商联调尚未执行。
核对日期：2026-11-08。本文保留已确认设计及设计时的代码基线；最终实施与验证结果见实施方案第 9 节。

## 1. 目标与范围

Run 表示用户一次任务从发起、执行、澄清到结束的生命周期。Session 承载持续会话，Message 保存对话及执行交互。

首期闭环：发送文本任务 → 数据库排队 → 独立 Worker 调用模型 → SSE 展示增量 → 原子保存完整回复并完成。支持多轮澄清、取消、失败重试、租约故障处理、模型配置版本及必要 Trace。

不包含：业务工具和子 Agent 实际接入、执行中用户追加或纠正指令、上下文压缩、多模态、跨用户运维内容查询、完整观测平台、前端页面实现。SSE 事件需足以支持前端展示、替换澄清文本和标记中断。

本节记录 Run 首期的范围。后续上下文压缩实施范围和当前规则见[上下文压缩设计](context-compression-design.md)与[实施方案](context-compression-implementation-plan.md)。

## 2. 设计时的代码基线与差异

以下记录设计确认前的代码基线及计划改动；最终实现状态以实施方案的验收记录为准。

| 位置 | 当前行为 | Run 所需改动 |
| --- | --- | --- |
| `backend/src/domain/session/model/entity.py`、`adapter/session/repository/session.py` | Session 有 owner、共享消息序号、软删除；更新和删除使用行锁 | 删除时在同一锁内检查未结束 Run |
| `backend/src/api/messages.py`、`app/message/service.py` | HTTP 只读历史，内部支持消息追加 | 新增 Run 提交入口，复用消息类型与校验 |
| `backend/src/adapter/message/model/conversation.py`、`repository/message.py` | client_message_id 幂等；还按 Session/run/role 唯一及查重 | 必须移除后者，支持同 Run 多条用户回答、助手澄清及最终回复 |
| `backend/src/domain/message/model/entity.py` | 两种流、run_id/thread_id/source_message_id 等已有定义 | 接入 Run 关联校验，不能仅凭字符串写入任意 Run |
| `backend/src/adapter/message/repository/message.py` | 每次 append 自开事务，在 Session 行锁内分配共享序号 | 增加接受同一事务上下文的内部写入能力；不能串联两个独立提交冒充原子操作 |
| `backend/src/main.py`、`app/llm/config.py`、`app/llm/service.py` | 命名部署 Profile，启动时载入；没有持久化版本管理 | 增加管理员版本/default 管理，Worker 按 Run 版本解析凭据 |
| `backend/src/adapter/llm/openai_responses.py` | 已有 Responses Adapter；工作树中还有未提交修改 | 与本次 Chat Completions 决策有差异，见第 13 节 |
| `backend/src/app/knowledge/worker.py`、`adapter/knowledge/repository.py` | 知识库已有独立 Worker、租约及 fencing token 模式 | 参考其模式，不复用其任务表、不改变知识库故障策略 |
| `pyproject.toml`、`adapter/models.py` | 有 OpenAI SDK，无 Redis 依赖及 Run 模型注册 | 新增 Redis 依赖、Run 模型、迁移和进程入口 |

当前分支为 `v2`。工作树存在其他 LLM/MCP 修改和文档，实施前重新核对并保护，不覆盖。原 Athena 仓库保持只读。

## 3. 已确认业务规则

- 每个 Session 最多一个未结束 Run；不同 Session 可以并行。
- 新任务创建 Run；澄清回答恢复原 Run。执行中不接受新指令。
- 等待输入不占 Worker 并发，仍占 Session 执行位置；cancelling 在停止或租约失效前占两者位置。
- 终态不可恢复。仅当前 Session 按 `created_at DESC, id DESC` 排序的最新 Run，且状态为 failed 时可由用户重试；重试创建新 Run 并关联 `retry_of_run_id`。成功请求的幂等重放始终返回原结果。
- 新 Run 在持有 Session 锁时，以数据库时钟和当前 Session 最大 `created_at` 生成严格递增的创建时间；应用时钟回退或历史时间较未来时，不改变“最新”的排序结果。
- 重试复用原触发消息、不复制用户消息；上下文截止原触发消息，不自动携带旧 Run 的澄清或执行历史。使用重试时默认模型版本。
- 未结束 Run 阻止 Session 删除。结束数据随 Session 软删除隐藏，不新增单独删除或物理清理。
- 管理员配置能力；普通用户使用默认模型。管理员不自动获得其他用户内容权限。

## 4. 数据归属与建议结构

以下表名、字段和端点随本文整体方案获确认；具体迁移、兼容及分阶段落点在实施方案中说明。

### 4.1 数据边界

| 数据 | 归属 |
| --- | --- |
| 用户任务、助手正式回复、澄清问题和回答 | Conversation message |
| 工具/子 Agent 原始调用与结果、恢复所需协议交互 | Execution message |
| 当前状态、关键时间、模型版本、最终错误、用量汇总 | Run |
| 排队、开始、等待、恢复、取消、结束的持久化历史 | Run event |
| 单次模型尝试耗时、请求标识、用量、错误及诊断 | Trace |
| 未完成的文本增量 | Redis/SSE 临时展示，不写正式消息 |

不保存模型内部思维链。助手整理后的用户回复与原始执行结果用途不同，分别保存。生命周期事件不混入模型上下文。

### 4.2 runs

建议字段：id、session_id、owner_id、status、trigger_message_id、context_cutoff_sequence、retry_of_run_id、model_config_version_id、queued_at、first_started_at、finished_at、created_at、updated_at、cancel_requested_at、safe_error_code，以及类型化 usage 汇总和 usage_complete 标识。

执行控制字段：execution_epoch（单调增长的执行代次）、worker_id、lease_expires_at、execution_started_at、execution_deadline_at。对外仅发送代次标识，不发送内部认证秘密。

约束与索引：

- 对 session_id 建部分唯一索引，条件为 queued/running/waiting_for_input/cancelling，数据库兜底禁止两个未结束 Run。
- 按 status/queued_at/id 建领取索引，按 status/lease_expires_at 建回收索引。
- 触发消息、重试来源必须属于同一 Session；重试来源还必须是该 Session 按 `created_at DESC, id DESC` 排序的最新 Run；模型版本必须存在。应用校验与适用的外键/组合约束共同保护。
- trigger_message_id 不是唯一键：多个失败重试共享原触发消息。
- 消息与 Run 可能互相引用，迁移采用允许事务内创建的延迟约束或明确的分步插入顺序，不依赖关闭完整性校验。

### 4.3 run_input_requests

id、run_id、question_message_id、status（pending/answered/closed）、answer_message_id、created_at、answered_at、closed_at。

每 Run 最多一个 pending 请求，question/answer 必须属于本 Session 且角色正确。一次请求可在 question 文本中包含多个问题，只接受一次非空文本回答，无自动超时。取消时将 pending 请求关闭。

### 4.4 run_events

id、run_id、sequence、type、created_at、类型化 payload。sequence 在 Run 内唯一、递增，与消息的 Session sequence 分开。状态变更与对应事件同事务。事件记录状态原因和消息/输入请求引用，不复制回复正文、凭据或异常堆栈。

### 4.5 run_requests：命令幂等

记录 owner_id/session_id/operation/request_id、规范化请求指纹、结果 Run ID/Message ID/Input request ID 和 created_at。operation 至少区分提交、回答、重试；回答指纹包含 input_request_id，重试指纹包含源 Run ID。

同作用域同键同内容返回原资源引用及其当前状态；同键不同内容返回 409。并发重复提交由数据库唯一约束兜底。先鉴权、再查幂等记录；已成功请求的重放优先于“当前已有未结束 Run”等新请求校验。记录与业务结果同事务，生命周期与 Session 一致。

### 4.6 模型配置与 Trace

模型配置版本保存 protocol、base_url、model、生成参数、上下文窗口、输出上限、能力声明、超时/执行策略及 credential_ref。版本内容不可变，enabled 为独立可修改的可用性状态。默认指向由独立配置记录管理，创建 Run 时事务读取并绑定可用版本。

Run 固定引用版本即可，不再复制一份可漂移的参数。被引用版本不得删除。管理员可停用：禁止新 Run 使用，已有 Run 下次模型调用前失败；已经发出的调用允许结束。密钥独立轮换、按调用解析，不能复制进 Run、事件或 Trace。配置查询不返回密钥。

Trace 首期采用结构化安全日志：trace_id/run_id/execution_epoch/attempt、耗时、上游请求 ID、实际模型、已知用量、安全错误码。无需先建通用 Trace 平台或 Trace 数据库。缺失用量保留未知，汇总表示“已知部分 + 是否完整”，不能把缺失值当零。

## 5. 状态机

| 原状态 | 目标状态 | 条件 |
| --- | --- | --- |
| queued | running | Worker 原子领取并取得租约、执行代次 |
| running | waiting_for_input | 完整合法的澄清响应原子提交 |
| waiting_for_input | queued | 回答、关闭请求、重新入队原子提交 |
| running | completed | 正式回复与完成原子提交 |
| running | failed | 模型/协议/配置/上下文错误或超时，执行已停止；或租约过期 |
| queued / waiting_for_input | cancelled | 直接取消，关闭待回答请求 |
| running | cancelling | 取消请求先提交，禁止新调用及正式输出提交 |
| cancelling | cancelled | 执行清理结束，或租约过期撤销旧执行权限 |

completed/failed/cancelled 为终态。重复取消返回当前状态，终态取消不改写结果。超时在清理完成前保留 running 并停止调度新调用，最终提交 failed；若取消先提交，按 cancelling 收尾，不能覆盖取消。

## 6. 原子操作与并发约束

业务事务统一按 Session → Run → 输入请求的顺序锁定；领取也遵守顺序，避免消息追加与取消、回收形成反向锁等待。不在持锁事务中调用模型或 Redis。

1. 提交：锁定可见 Session → 查幂等 → 校验无未结束 Run及默认版本 → 保存 user 消息、Run、queued event、幂等记录 → 提交。
2. 回答：锁 Session/Run/请求 → 查幂等 → 校验 waiting+pending → 保存回答、协议 tool result、请求 answered、Run queued、新 queued_at、恢复事件及幂等结果 → 提交。
3. 完成：锁 Session/Run → 校验 running、代次匹配、数据库时钟下租约未过期 → 保存完整 assistant 消息、usage、completed event 和终态 → 提交 → 发布通知。
4. 澄清：同样校验执行权限 → 保存正式问题、协议调用消息、输入请求、usage、waiting event → 清除租约并提交 → 发布通知并退出当前执行。
5. 取消：锁 Session/Run → queued/waiting 直接结束；running 改 cancelling → 保存事件 → 提交。Worker 通过数据库轮询发现取消，Redis 通知仅用于加速。
6. 删除 Session：锁 Session → 检查未结束 Run → 有则 409，无则沿用软删除。提交和删除竞争必须在同一锁下串行。

完成和取消谁先成功转换状态谁生效。旧 Worker 的消息、输入请求、执行记录、usage 和状态提交全部校验代次及租约，不允许绕过仓储内部入口追加。回收与完成竞争也按同样规则。

## 7. Worker、租约、超时与重试

独立 `backend/src/run_worker.py` 进程，数据库轮询 queued，按 queued_at/id 领取；使用短事务、行锁与 SKIP LOCKED 避免多 Worker 重复领取。并发领取时顺序是尽力先进先出，不保证执行完成顺序。回答澄清重新入队不保留旧排名。

Worker 本地最大并发控制领取；首期无用户配额和优先级。领取时生成新 execution_epoch，设数据库时钟租约和本轮 deadline，定期续约；不能续活已过期租约。执行期间续约与取消检查独立于流消费及 Redis 发布。

定期回收过期 running → failed(execution_interrupted)，cancelling → cancelled；撤销旧代次写入权限，记录事件，不自动重放。queued/waiting 不受 Worker 重启影响。回收职责由 Worker 定期扫描承担，可多实例竞争执行；全部 Worker 下线期间，需等进程恢复才能回收。

正常取消尝试中断上游，停止新调用，清理结束后 cancelled；不回滚外部效果。失联时 cancelled 代表撤销写权限，不保证远端请求已物理停止。旧 Worker 一旦失去租约应主动关闭流并释放本地资源。

排队、等待输入无超时。每次领取独立计时，模型另有请求/流时限，重试退避包含在执行时限内；首期无跨澄清累计上限。管理员配置这些参数。

模型临时网络、限流、临时服务端错误最多尝试三次（含首次），指数退避并尊重上游等待提示，但不能越过 deadline。仅尚未收到任何模型输出时重试；文本、调用开始或参数增量任一出现后，中断均失败。认证、参数、协议、上下文超限不重试。SDK 隐藏重试关闭。每次尝试独立 Trace，取消/超时中断退避。

## 8. 上下文与响应协议

新 Run 查询本 Session 的 Conversation message，sequence 不超过 context_cutoff_sequence，首期文本输入；不读取其他 Run 的 Execution message。此前正式澄清问答、失败或取消前已保存的用户可见回复仍在历史中。

恢复使用原截止上下文，加本 Run 已提交的澄清协议交互和用户回答。不能同时将正式 question 的对话投影和其 tool-call 投影重复加入同一次输入。建议持久化 assistant request_user_input ToolCallPart 及用户回答对应的 TOOL ToolResultPart，通过 call_id 和 source_message_id 关联正式消息；恢复时重建完整合法的调用/结果对。首期使用稳定 root thread_id，不引入子执行实体。

不压缩、不静默截断。模型版本声明上下文窗口、输出预留和可用 tokenizer；发送前估算包含系统提示、工具 Schema、协议及全部输入的大小。超过预算失败；服务端返回超限也失败。未知模型无法精确计算时须说明估算限制，不能以字节限制替代 Token 窗口；具体 tokenizer 映射需联调验证。

Run 使用明确的系统提示描述澄清及完成协议；提示模板应有稳定版本，Run 创建时固定模板版本，恢复不随部署任意改变。

首期目标协议为 Chat Completions，不探测、不自动回退：

- 支持流文本、function calling 和明确结束原因，关闭 parallel_tool_calls。
- 专用动作 request_user_input，仅接收非空 question 字符串，拒绝额外字段。
- 最多一个调用，且必须是该动作。完整参数校验和整个响应正常结束后才提交等待。
- 有动作时 question 是唯一正式澄清文本；附带普通文本不单独保存，前端用正式问题替换临时文本。
- 无动作时非空文本、正常结束才完成。
- 截断、内容拦截、未知/多个动作、参数不完整、缺结束事件、异常断流均失败，不保存半成品正式回复。

## 9. Redis 与 SSE

Redis Pub/Sub 仅传递通知和临时增量，不调度任务、不持久化 Token。Worker → run 频道 → API → SSE。Run 执行与数据库保存不依赖订阅者存在。

建议统一事件 envelope：run_id、execution_epoch、stream_sequence、type、payload。增量仅含展示文本；正式通知携带 message_id、run event sequence 等资源引用。事件类型至少包括 snapshot、text_delta、message_saved、input_required、run_state_changed、stream_interrupted。

连接鉴权并订阅后，以一致数据库快照读取当前状态、正式消息和事件游标；先发 snapshot，再处理缓冲通知，按 message ID/event sequence 去重。每条临时增量须校验该执行代次仍有效、租约未过期且 running；检查失败时丢弃。状态读取须保证不会因过期本地缓存持续接收旧 Worker 输出。

实时检查与网络发送间仍可能发生取消，因此客户端将所有 delta 视为临时展示，以正式消息/状态对账，不能承诺撤回已发出的字节。终态或等待通知清理/替换当前临时输出。

Redis 通知可能丢失。SSE 除心跳外定期从数据库对账正式消息和状态，保证提交成功但发布失败时仍能收尾。订阅断开后重订阅并重新快照；客户端重连同样快照，不按 Last-Event-ID 回放 Token。明确告知临时流中断，不将剩余后缀伪装为完整回复。

Redis 发布与订阅使用有界缓冲和超时；慢客户端可断开重连，发布失败不阻塞心跳/取消或导致 Run 失败。Redis 不可用时仍允许查询正式结果。

SSE 建连使用当前登录身份，不在 URL 放密钥；长连接定期复核登录有效性及 Session 可见性，失效关闭。心跳与代理缓冲/超时配置在部署说明中落实。

## 10. API 建议

沿用 CurrentAuth 与写操作 Origin 校验。跨 owner 或已删除 Session 统一 404；已鉴权业务状态冲突为 409，非法输入为 422。

| 方法与路径 | 行为 |
| --- | --- |
| POST /api/v1/sessions/{session_id}/runs | request_id + text，原子创建任务消息及 Run |
| GET /api/v1/sessions/{session_id}/runs | 游标分页列举本 Session Run |
| GET /api/v1/sessions/{session_id}/runs/{run_id} | 状态、时间、触发消息、重试来源、模型版本公开投影、usage、安全错误及当前输入请求 |
| POST /api/v1/sessions/{session_id}/runs/{run_id}/cancel | 幂等请求取消，返回当前状态 |
| POST /api/v1/sessions/{session_id}/runs/{run_id}/retry | request_id，创建新 Run |
| POST /api/v1/sessions/{session_id}/runs/{run_id}/input-requests/{request_id}/answer | 独立幂等键 + text，回答并恢复 |
| GET /api/v1/sessions/{session_id}/runs/{run_id}/events | 按 after_sequence 正序分页 |
| GET /api/v1/sessions/{session_id}/runs/{run_id}/stream | SSE 快照与实时事件 |

模型管理另设 admin 路由：创建/读取版本、启停版本、读取/设置默认版本。普通用户不能配置；Run 详情不返回 endpoint、credential_ref 或其他配置秘密。无有效默认配置时提交失败，不留下孤立用户消息。完整命令返回 Run 与相关 message/input request 引用。

## 11. 分层与实施顺序

沿用 api → app（按领域编排）→ domain（实体、端口）→ adapter（显式实现端口）。Run 新代码放在 domain/run、app/run、adapter/run，各自按现有 model/repository/converter 结构组织；API 新增 runs 及 DTO/converter。模型版本管理归 llm 领域，不混入 Run repository。Redis 实现在基础设施/适配边界，domain 不依赖 Redis 客户端。

日期和 JSON 复用 common/date.py、common/json.py。数据库 JSON 必须有明确 Pydantic 类型和序列化边界，不使用任意字典承载持久化协议。

已确认的设计实施顺序如下，具体执行拆分见实施方案：

1. 模型协议差异确认，补齐模型版本及默认配置管理；扩展 LLM 端口的版本配置解析和并行调用控制，不破坏既有 Responses 调用方。
2. Run/请求/事件/幂等实体、迁移、事务写入；移除旧消息 run-role 唯一规则，保留客户端幂等和共享消息序号。
3. 提交、回答、取消、重试、查询、Session 删除保护及所有者隔离。
4. Worker 领取/续约/回收、上下文、模型响应判定、有限重试及安全 Trace。
5. Redis/SSE、快照对账、慢客户端和断线处理；补部署与配置说明。
6. 真 PostgreSQL 并发测试、Redis 集成测试、实际模型兼容性联调、独立 Reviewer 审查，Root 最终验收。

迁移使用执行时最新 Alembic head，不能按本文日期猜编号。保留历史消息，移除限制前核对真实约束名。历史 run_id 当前无实体来源，不能盲目添加外键；迁移前审计并制定保留映射/兼容策略，不静默删除。允许旧消息为空关联，新 Run 写入严格校验。明确生产数据升级和降级可行性，不能仅以测试库重建替代升级验证。

## 12. 验收标准

- 同 Session 并发提交只有一个任务成功，失败者不留下消息；不同 Session 可并行。
- 提交/回答/重试并发幂等：同键同内容只生效一次，同键异内容 409；成功命令重放不被当前运行状态误拒绝。只能重试当前 Session 最新 Run；其后有更新 Run 时，新重试请求被拒绝。
- 多轮澄清可跨 Worker/进程恢复，无旧 run-role 唯一冲突；重复/迟到回答不能二次恢复。
- 完成/取消、回答/取消、删除/提交、续约/回收竞争满足锁顺序和原子性；数据库回滚不产生半状态。
- 多 Worker 不重复领取；等待释放本地名额；恢复重新排队；cancelling 不提前释放。
- 租约过期后旧 Worker 不能写任何业务结果；running 失败、cancelling 取消；不自动重新执行。
- 上游取消清理、超时、退避中取消都可结束；取消/超时后不再发起新模型请求。
- 流输出后异常不重试；无输出临时错误最多三次；未知 usage 不填零。
- 文本与动作混合、多动作、未知动作、参数分片、截断、拒答、空输出、断流均按已确认规则处理。
- 失败重试只读原截止上下文；澄清恢复保留自身协议现场、不重复消息、不读取其他 Run 执行历史。
- 上下文预算包含工具/系统/输出预留；本地与服务端超限均明确失败，无自动截断。
- 模型默认切换不影响已有 Run；停用阻止新调用；密钥轮换生效且不泄漏。
- Redis 失败不影响正式完成；提交后发布失败可通过对账发现；连接竞争不漏正式结果；重连不承诺 Token 回放。
- 慢客户端、Redis 断开不阻塞 Worker 租约及取消；旧代次事件被过滤，临时文本不冒充正式消息。
- 跨用户查询、订阅及写操作不可访问；失效登录断开 SSE；软删除后全部用户入口不可见。
- 既有 Session/Message/LLM/知识库测试无回归；真实 PostgreSQL 验证行锁和部分唯一索引，不以 SQLite 替代。实际模型和 Redis 未验证时明确标记，不宣称闭环验收完成。

## 13. 实施准备情况与后续事项

用户已整体确认设计并要求整理实施方案；当前尚未授权开始代码实现：

1. **协议差异已确认处理方式**：保留当前 Responses 能力，新增 Chat Completions Adapter 供首期 Run 显式使用，不替换既有实现、不探测协议、不自动回退。后续实施应同步相关 LLM 文档，明确两种 Adapter 的使用边界。
2. **本文整体方案已确认**：表结构、端点、澄清协议持久化、SSE 对账及模型管理纳入设计基线；实施方案单独列明执行步骤和进一步的兼容落点，设计确认不等同于本轮代码实施授权。
3. **环境参数在实施/联调前落实**：实际模型/base_url/凭据、tokenizer 映射、Redis 地址，以及 Worker 并发、租约/续约/轮询周期、执行超时、缓冲和请求大小上限。秘密值不写文档；参数需有可校验默认值及范围。
4. **历史数据和工作树在实施前再审计**：保护现有未提交工作，检查历史 run_id 与最新 migration head。没有证据时不承诺可无条件添加外键。

本文仅是设计产物，不代表 Implementer/Reviewer 已执行。实施方案确认并进入开发后，遵循 Root → luna-implementer → terra-reviewer → Root；有效问题回到原 Implementer 修复并重新 Review，最多三轮，不跳过独立审查。
