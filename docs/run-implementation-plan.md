# Run 首期实施方案

状态：设计方案已获用户整体确认；S1–S6 已完成实施、独立审查及 Root 验收。真实供应商联调尚未执行。
依据：`docs/run-design.md`。代码核对日期：2026-11-08，分支：`v2`。

## 1. 交付目标与约束

交付后端完整闭环：提交文本任务 → PostgreSQL 排队 → 独立 Worker 调用 Chat Completions → Redis/SSE 展示增量 → 原子保存完整回复并结束。包含多轮澄清、取消、失败重试、租约回收、模型版本管理和安全 Trace。

保留已有 Responses Adapter 和部署 Profile 调用路径；首期 Run 显式使用新增 Chat Completions Adapter，无协议探测或自动回退。不接入业务工具、子 Agent、执行中追加指令、上下文压缩、前端页面或通用 Trace 平台。

以上为 Run 首期实施范围记录。后续上下文压缩方案与实现状态见[上下文压缩实施方案](context-compression-implementation-plan.md)，其中的策略、预算、事件和失败规则适用于后续 Run 执行。

仅在 Athena_v2 修改。当前工作树已有 Responses、MCP 未提交修改及其他文档，实施前记录基线，不覆盖、不代为提交其他工作。原 Athena 仓库只读。

沿用 api/app/domain/adapter 分层。新 Adapter 显式实现端口，持久化 JSON 用明确 Pydantic 类型；日期、JSON 公共能力放在既有 common/date.py、common/json.py，不另建通用框架。

## 2. 阶段与依赖

| 阶段 | 产物 | 前置条件 | 阶段完成判据 |
| --- | --- | --- | --- |
| S1 模型基础 | 版本管理、默认配置、动态凭据、新协议适配 | 本实施方案确认 | 已通过：配置隔离、协议模拟测试、Responses 回归及独立审查 |
| S2 Run 持久化 | 状态、输入请求、事件、幂等、共享事务和迁移 | S1 模型版本契约 | 已通过：隔离 PostgreSQL 原子性、约束、并发测试及独立审查 |
| S3 管理接口 | 提交、回答、查询、取消、重试、删除保护 | S2 | 已通过：ASGI 权限、幂等、多轮澄清与删除保护测试及独立审查 |
| S4 执行闭环 | Worker、上下文、澄清、租约、取消和重试 | S1–S3 | 已通过：执行器、上下文、Worker 生命周期及 PG 执行闭环审查 |
| S5 实时传输 | Redis Pub/Sub、SSE 快照和对账 | S3–S4 | 已通过：真实 Redis 收发、SSE 快照分页、丢通知和中断语义审查 |
| S6 集成验收 | 迁移、回归、真实依赖联调和部署说明 | S1–S5 | 已通过：真实 PostgreSQL/Redis/HTTP/fake provider 联合闭环、全量回归、最终独立审查及 Root 验收 |

由同一 Implementer 按顺序推进，阶段完成先交付结果及 Diff，经 Reviewer 审查、Root 验收后推进；不要求为每阶段重新取得业务规则批准。修复复用原 Implementer 上下文，最多三轮修复复审。最终还要审查跨阶段集成 Diff。

## 3. S1：模型版本与 Chat Completions

### 3.1 修改位置

- `domain/llm/model/`、`domain/llm/ports.py`：增加不可变模型配置版本、凭据引用和配置仓储契约；补充 Run 调用所需配置输入、关闭并行调用的控制和安全错误分类。
- `app/llm/`：模型版本管理用例、默认选择、启停校验、凭据解析及调用前可用性检查。保留当前 LlmService/Profile 接口供既有调用方使用。
- `adapter/llm/model/`、`repository/`、`converter/`：新增配置版本及默认指向的持久化。数据库 JSON 明确类型，不存解析后的 API Key。
- `adapter/llm/openai_chat_completions.py`：新增 Run 所需流式能力及工具调用重组，显式实现适合该能力的端口。不要为凑齐既有完整 LlmProvider 接口实现无需求的结构化能力；必要时拆出最小流式端口，让既有 Provider 兼容继承，避免影响旧调用方。
- `api/llm.py`、对应 DTO/converter：管理员创建/读取版本、启停、读取/设置默认；复用 AdminAuth，并按现有约定在管理服务执行最终管理员身份复核。
- `main.py`、`adapter/models.py`：注册配置服务、路由及模型；客户端资源按创建者负责关闭。

模型版本固定 model/base_url/protocol/参数/窗口/tokenizer/输出上限/调用与执行时限。enabled 独立可变。默认指向事务更新；提交 Run 与停用/切默认竞争须锁定相关配置，最终只绑定一个可用版本。配置锁位于业务 Session/Run 锁之后，管理操作不得反向获取 Session/Run 锁。

credential_ref 首期使用受控环境变量名，管理接口不接收密钥正文。Worker 每次调用解析最新值；进程环境变量轮换需更新并重启 Worker，等待中的 Run 仍引用原配置版本。客户端连接池按配置与凭据身份区分，轮换后不能复用旧密钥客户端。实际地址及凭据部署时提供。

不把数据库配置反灌到全局 Profile 字典，不通过共享可变配置让并发调用串用。SDK 保持 max_retries=0，Run 负责重试。

### 3.2 验证

覆盖权限撤销、版本不可变、默认切换、停用并发、凭据隔离及日志脱敏；模拟流覆盖分片、混合文本/调用、多个或未知调用、空输出、异常结束、usage 缺失、取消和提前关闭。Run 适配所需错误包含可安全判断的错误类别及 retry_after，不向用户暴露原始上游错误。

真实 SDK 字段及资源关闭方法在实施时根据当前固定版本核验；兼容服务能力单独联调。同步 llm-design.md/llm-implementation-plan.md 的当前协议边界，不重写其历史为“原本就支持两种协议”。

## 4. S2：数据与事务

### 4.1 位置及数据

新增 `domain/run/model/`、`domain/run/ports.py`、`adapter/run/model/`、`repository/`、`converter/`，注册于 adapter/models.py。按设计新增 runs、run_input_requests、run_events、run_requests。

Run 补齐 prompt_template_version、事件计数及设计中列出的字段。固定提示模板随代码版本发布，旧版本在仍有等待 Run 时保留，避免恢复时静默改变指令。Run event 使用 discriminated union payload；usage 明确已知部分和完整性。

PostgreSQL 约束包括：每 Session 至多一个未结束 Run、每 Run 至多一个 pending 输入请求、Run 内事件序号唯一、命令幂等键唯一、有效状态检查、同 Session 引用关系。新 Run 创建前生成 Run/message ID，先写 trigger user message（暂不赋 run_id）、再 Run、再回填 message.run_id，全部同事务，避免循环外键插入问题。

### 4.2 共享事务落点

保留 MessageRepository 现有公开 append 行为供原调用方使用；在 `adapter/message/repository/` 抽出接收已有 AsyncSession 的内部写入帮助函数，负责原有消息校验、序号分配及转换，不自行提交。

RunRepository 为业务原子命令提供明确端口，例如 submit、answer、finish、request_input、cancel、retry，而不是把 AsyncSession 暴露给 app/domain。Run Adapter 在一笔事务中复用消息内部写入；Session 删除仓储同锁检查未结束 Run。不要建立项目级通用锁或通用 Unit of Work 框架。

移除 Conversation 的 uq_conversation_messages_run_role 和仓储对应 run-role 查重，保留 client_message_id 幂等。Run 命令幂等和状态转换防止重复最终回复/澄清提交；不能仅删约束而保留旧查重逻辑。现有消息草稿和测试的 run-role 假设同步调整。

### 4.3 迁移与历史消息

实施时读取真实 Alembic head，再生成迁移，不预定编号。先创建模型配置表，再 Run 表及关联约束，最后调整旧消息唯一约束。

历史 run_id 没有可验证来源，不自动虚构 Run，也不清空原值。建议新增可空的内部 managed_run_id 外键列：历史行保留原 run_id、managed_run_id 为空；本期新 Run 写入两者且相等，并用 managed_run_id 联合 Session 约束归属。用户可见和既有协议仍使用 run_id，内部新写入不能绕过 managed 关联校验。该兼容落点已随本实施方案确认并实施。

迁移测试同时覆盖空库及带历史消息的升级。旧 run-role 唯一规则无法容纳多轮澄清数据，降级必须在有冲突数据时明确拒绝并提示备份恢复，不能删消息凑约束。部署前对目标数据库只读审计，实施阶段不要直接操作生产数据。

### 4.4 验证

故障注入覆盖消息写入后/事件写入前抛错，所有关联数据一起回滚。真实 PostgreSQL 验证部分索引、外键、并发幂等、同 Session 限制及历史升级。SQLite 只支持不依赖 PostgreSQL 语义的测试。

## 5. S3：用例与 HTTP

新增 `app/run/service.py`、结果 model/converter、`api/runs.py`、DTO/converter，端点和响应遵循设计第 10 节。列表使用稳定时间/id 游标，event 使用 after_sequence；详情返回当前输入请求，正式消息复用已有历史查询。

提交、回答、重试先校验 owner/未删除 Session，再查幂等；已成功重放不能被当前状态拒绝。同键异内容 409；未结束 Run、重复/迟到回答及重试非最新 Run 等业务冲突 409；跨 owner 404。重试在 Session 行锁内按 Run 列表使用的 `created_at DESC, id DESC` 顺序确认来源是最新 Run。提交和新重试也在锁内使用数据库时钟与会话最大创建时间生成严格递增的 `created_at`，避免应用时钟漂移令后创建 Run 排到旧 Run 前。回答路径的 input request ID 与命令幂等键在 DTO 中分别命名，避免混淆。

失败重试沿 retry_of 链保留直接来源，但 trigger_message_id/context_cutoff_sequence 始终来自原任务，采用当前默认模型版本和当前提示模板版本，不复制用户消息。仅当前 Session 最新且状态为 failed 的 Run 可重试；成功请求的幂等重放不受后续 Run 影响。

取消 queued/waiting 直接终结并关闭 pending 请求；running 仅进入 cancelling。Session 删除与创建 Run 在 Session 锁下串行；终态取消返回当前终态。

测试覆盖所有端点的身份/Origin、参数、安全错误投影、相同/冲突请求重放、多轮回答及软删除可见性。不通过新增通用 message 发送端点绕开 Run。

## 6. S4：Worker 与执行闭环

新增 `app/run/worker.py`、`executor.py`、`context.py` 及必要配置/提示模板；入口 `backend/src/run_worker.py` 独立组装数据库、模型配置、凭据解析、LLM 和事件发布端口。发布端口尚未接 Redis 时可注入测试替身，不改变执行语义。

### 6.1 领取和锁顺序

统一 Session → Run → Input request，涉及配置时在这些锁之后锁配置。领取先无锁枚举少量 queued 候选，逐个尝试 `Session FOR UPDATE SKIP LOCKED`，再锁并重查 Run 是否仍 queued、Session 是否有效，再原子领取。不要先锁 Run 再锁 Session。候选过期/竞争失败继续扫描，不将正常竞争当执行失败。

回收、完成、澄清、取消、续约也遵守相同顺序；只更新 Run 的路径不能随后反向请求 Session。用数据库时钟判断租约，心跳不能续活过期租约。领取递增 execution_epoch；所有业务写入检验 epoch/状态/未过期租约。

### 6.2 执行

有界 consumer 数量控制领取，不预领超出空闲名额的任务。上下文按原始截止序号读取全部分页，不误用只读历史默认的 20 条；恢复叠加本 Run 的协议交互，不重复 question 的对话与 tool-call 投影。稳定 root thread_id，不引入子执行表。

tokenizer 配置采用显式已支持映射；新增所需计数依赖并锁定兼容范围。对未知映射拒绝启用为 Run 默认配置，不用字符/字节数假装精确 Token。预留系统提示、Schema、协议开销和输出空间，实际服务超限仍明确失败。服务差异与计数误差在部署说明中注明。

首期 FileRefPart 只按文件名加入上下文占位，不读取附件正文；附件摄取与模型可见内容需后续单独设计。

每次流最多一个 request_user_input；参数严格校验，完成响应后原子保存 question/调用/request/waiting。回答事务保存 user message 和对应 tool result。最终回复事务保存 message/usage/completed event。整个过程不把临时增量写入正式消息。

### 6.3 控制与故障

续约、取消轮询、deadline 监控与模型流/发布独立运行。取消或超时关闭流、停止新调用并等待资源清理；正常取消结束后 cancelled，超时清理后 failed。取消先提交时超时不能覆盖 cancelling。旧 epoch 或租约失效立即关闭本地流，丢弃所有结果。

Worker 用数据库领取返回的绝对 deadline 映射本地 monotonic deadline，避免领取后处理延迟重置执行时限。跨机器的墙上时钟仍可能偏差；数据库是写入租约与 deadline 的最终裁决端，部署节点需保持时钟同步。

Worker 定期回收 expired running→failed、cancelling→cancelled，不重新排队。优雅退出先停止领取、清理活跃任务；不能确认安全落库时停止续约，交由回收判定中断，不冒充任务完成。

临时错误最多三次；一旦收到任何输出，不再自动重试。退避可取消且受 deadline 限制。usage 在每次已知尝试结束时以 execution_epoch/attempt 幂等记账，避免最终提交失败重放后重复累加；缺失保持未知。必要记账字段位于 Run 的类型化执行汇总，不新增通用 Trace 表。

建议初始部署默认值（本方案参数，非既定产品规则）：并发 4、租约 30 秒、续约 5 秒、取消轮询 1 秒、队列轮询 1 秒、回收扫描 5 秒、单轮执行 300 秒、模型调用 120 秒；模型配置可收紧，等待配额/重试/流消费都不能越过单轮期限。启动校验续约小于租约、所有超时/并发为正且有上限。

### 6.4 验证

模拟流与可控时钟验证：直接完成、两轮澄清跨 Worker 恢复、输出后断流、无输出退避、退避中取消、超时、失租、终态竞争。PostgreSQL 并发验证双 Worker 领取、旧执行恢复、回收与完成、回答与取消、删除与提交，使用同步屏障构造真实竞争，不依赖长 sleep 碰运气。

## 7. S5：Redis 和 SSE

新增 `domain/run` 事件传输端口、`adapter/run` Redis 实现及 `app/run` 流服务；Redis 客户端依赖在 pyproject.toml 声明。API 负责 HTTP/SSE 编码，不把供应商事件名作为前端协议。

统一强类型 envelope：run_id、execution_epoch、stream_sequence、type/payload。订阅后获取一致数据库快照，发 snapshot，再处理有界缓冲；正式资源按 message ID/event sequence 去重。对临时 delta 校验当前 epoch、running 和数据库时钟租约；首期可每条/有界批次查询权威状态，不使用可能持续过期的本地状态缓存。合批只用于减少查询和网络开销，不持久化 Token。

Worker/API 可选配置 `ATHENA_REDIS_URL`。Run 快照由 PostgreSQL Repeatable Read 事务读取；每页最多包含 8 条最近正式消息、100 条最早状态事件且最多 512 KiB，并返回 `messages_complete/events_complete` 与 `messages_before_sequence/events_after_sequence` 游标。超出部分可用既有消息与事件分页接口读取，不静默裁掉；初次 Pub/Sub 预读最多 256 条、1 MiB，单条 Redis 通知上限 64 KiB。

周期数据库对账保证正式提交但 Pub/Sub 发布丢失时也能发现结果。Redis 重订阅或客户端重连都重新快照，通知 stream_interrupted，不按 Last-Event-ID 回放 Token。等待/终态替换或清理临时显示。

长连接定期重验实际 Cookie 登录态和 Session 可见性，而非只保留建连时 AuthContext。未登录建连 401，连接中失效则安全结束。推送仅返回当前用户有权限看到的数据。

建议初始默认值：SSE 心跳 15 秒、数据库对账及认证复核 5 秒、发布超时 1 秒、每连接缓冲最多 256 个事件且 1 MiB、单次文本合批最多 50 ms 或 4 KiB。到界限断开慢客户端，不阻塞 Worker 心跳/取消；明确统计丢弃/断连，不静默无界积压。

Redis 不可用仍可启动 Run 执行和查询，实时服务降级为数据库对账并发流中断通知；Redis 凭据不输出到日志。main.py 和 Worker 入口对各自创建的 Redis/LLM/DB 资源独立清理，一项关闭失败不阻止其他关闭。

测试覆盖订阅与提交竞争、发布失败、Redis 中断重订阅、慢客户端、旧 epoch、终态漏通知、登录撤销和跨用户。关键路径另用实际 Redis 测试，不把内存替身当 Pub/Sub 集成证明。

## 8. S6：检查、联调与交付

实施时增加以下测试组（名称可随现有目录组织微调）：

- `test_run_service.py`、`test_runs_api.py`：命令、幂等、权限和 HTTP。
- `test_run_worker.py`、`test_run_context.py`：执行、上下文、模型输出判定、控制和用量。
- `test_run_stream.py`：SSE 快照、对账、有界缓冲、认证。
- `test_llm_chat_completions.py`、模型版本 service/API 测试：协议与配置。
- `integration/test_runs_postgres.py`、`test_run_migrations_postgres.py`、Redis 集成测试：真实锁、约束、迁移和传输。

沿用当前 `.venv` 执行受影响测试，再执行全量 pytest、ruff check、ruff format --check、mypy，具体范围按 pyproject.toml。先记录已有失败，区分新回归；对未修改文件的既有格式问题不做无关批量修复。检查完成后仅在新变化或新失败出现时重复。

真实闭环至少验证：普通完成；request_user_input 后回答恢复；流中取消；客户端断开后任务完成；Redis 中断仍保存结果；Worker 停止后按租约回收；失败重试绑定新默认版本。记录实际模型、协议、服务兼容性和测试结果，不记录凭据。环境未提供时推进模拟与其他独立工作，最终明确未验证项，不宣称端到端验收完成。

同步 README、.env.example：数据库迁移、管理员配置默认模型、API 和 Run Worker 独立启动、Redis、代理禁缓冲及 SSE 超时、凭据轮换、旧配置保留、故障恢复。增加实际使用的测试 marker，并确保无数据库/无模型配置时现有健康检查仍可用。

Reviewer 独立查看实际阶段 Diff 和累计 Diff，核对设计、并发、异常、数据一致性及测试。每条 Finding 必须含文件位置、触发条件、影响及修复建议；只审查不改代码。Root 验证有效问题，交原 Implementer 修复后复审；PASS 才验收，不以测试通过替代审查。

## 9. 最终整合状态

S1–S6 已完成实施、Reviewer 独立审查及 Root 验收。最终修复处理 tokenizer 线程启动后超时的容量归属和迟到异常：线程成功启动后仅由实际退出的线程释放槽位，调用方超时取消结果 Future；启动失败则由调用方释放槽位。

2026-11-08，Root 在最终代码上使用隔离 PostgreSQL、Redis 执行全量 pytest：491 项通过、3 项跳过、25 条警告。联合闭环覆盖公开 Cookie 认证的 ASGI API、真实 Run 仓储、Worker 和 Redis Pub/Sub/SSE；模型端使用确定性 fake streaming provider。全仓 mypy 检查 280 个文件通过，Ruff check 与 git diff --check 通过，本轮修改文件格式检查通过。全仓格式检查仍有 knowledge 测试与 LLM 设计文档中的既有差异，未做无关批量格式化。

`README.md` 和 `.env.example` 已记录 Worker 参数、管理员模型配置、凭据前缀约束、Redis 降级、SSE 代理要求、tokenizer/prompt 预算及迁移限制。真实模型供应商联调尚未执行，以上结果不代表具体供应商兼容性验证或生产部署验收。首期不包含前端页面、业务工具和子 Agent 接入。未提交、推送或部署代码。
