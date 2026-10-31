# Worker HTTP 协议 v1

日期：2026-10-30

状态：S1 接口协议定稿；目标实现规范，当前后端和 Worker 尚未实现此协议。本文与 [worker-registry-round-plan-http-design.md](worker-registry-round-plan-http-design.md) 配套。本文中的接口路径、字段、状态码、错误目录和配置表对 S2–S8 有约束；协议 body 以 [worker-protocol-v1.schema.json](worker-protocol-v1.schema.json) 为准，示例以 [worker-protocol-v1.examples.json](worker-protocol-v1.examples.json) 为准。

## 1. 一致性规则

JSON Schema 使用 JSON Schema Draft 2020-12。Schema 文件中的每个 $defs DTO 是共享请求或响应 body；示例文件的 examples 对象中，每个正例 payload 都通过 schema_ref 指向该 DTO，rejected_examples 是必须被拒绝的反例，lifecycle_scenarios 覆盖需跨请求验证的身份/状态边界。body 中未声明的字段拒绝。客户端和服务端必须使用相同的 schema、字段语义和错误动作。

Schema 无法单独验证 HTTP 头、路径/body 绑定、服务身份、动态能力 Schema、数据库中的当前代次和内容摘要；本页这些规则同样是协议的一部分。

幂等操作 ID（registration_request_id、command_id、approval_request_id、result_id）、worker_instance_key、operation_id、pause_id、item_id 和 resource_id 都使用 UUID。Athena 生成的 worker_instance_id、Run/Plan/Task/Execution/Contract/Approval/Receipt ID 是不透明字符串，长度为 1–255 个 ASCII 字符，格式为首字符字母或数字，其余字符只允许字母、数字、点、下划线、冒号和连字符；客户端不得解析其业务格式。时间使用 RFC 3339 UTC 时间戳。协议版本固定为 1.0。HTTP JSON body 使用 UTF-8 和 application/json。生产网络只允许 HTTPS。

每条有副作用的请求先做身份认证与协议版本检查，再做 DTO 校验、路径和当前执行绑定、幂等摘要比对，最后执行状态转换。状态转换及其持久回执必须在同一事务中提交。只要响应可能丢失，客户端就用相同 ID、相同请求体重投。

## 2. 通用 Header、身份和幂等

### 2.1 请求头

| Header | 必填 | 规则 |
| --- | --- | --- |
| Authorization | 所有接口 | 使用独立服务 Bearer 凭据。Athena 服务端识别 Worker 服务主体；Worker 接收端仅接受 Athena Orchestrator 主体。注册 ID、实例 ID、命令 ID 不能当凭据。 |
| X-Athena-Execution-Grant | 资源 PUT/GET | 必须带 TaskCommand/ResumeCommand 中由 Athena 签发的 opaque execution_grant.token。服务 Bearer 认证主体，Execution Grant 授予具体 Execution 的资源范围；两者不可互相替代。 |
| X-Athena-Protocol-Version | 所有接口 | 值固定为 1.0。未支持时返回 PROTOCOL_VERSION_UNSUPPORTED，不做猜测式兼容。 |
| Content-Type | JSON 请求 | 必须为 application/json; charset=utf-8。资源传输使用资源媒体类型。 |
| Idempotency-Key | 所有有副作用的 POST/PUT | UUID；必须等于下表指定的 body 字段或资源 URL ID。GET 不带该头。 |
| Retry-After | 可选响应头 | 秒数且大于 0；客户端将其作为退避下限。JSON 错误体可重复返回有限正数 `retry_after_seconds`，同时出现时采用两者中较大的下限。若等待会越过执行 deadline，Orchestrator 停止重发并等待结果或 deadline 处理。 |

请求/响应 JSON 中也必须有 protocol_version: "1.0"。服务身份与实例身份分开管理。凭据轮换不能更换或复用 worker_instance_key；并行服务实例必须有各自持久的实例键。

同一认证主体加 worker_instance_key 唯一确定可恢复实例。Worker 重启沿用相同 key、服务身份和本地持久记录，向注册接口重新登记。registration_request_id 标识一次注册请求：响应丢失时重投原 ID 和原 body；实例配置变化时生成新的 ID。相同请求 ID 与不同内容返回 409。能力内容不变时沿用 contract_id 和 contract_revision；能力内容改变时创建不可变的新 revision。修改超时或并发上限只影响新分配的 Execution。

所有 JSON 幂等请求用 RFC 8785 JCS 规范化 body 后计算 SHA-256。相同 key、相同摘要返回原业务回执；相同 key、不同摘要返回 409 冲突，不允许覆盖第一份内容。幂等回执至少保留到所属业务记录过期；registration_request_id 保留到实例身份退役。资源 PUT 以 resource_id 和内容 Digest 执行同样的同内容重放规则。

幂等 key 对照：

| 操作 | Header Idempotency-Key | 稳定操作 ID |
| --- | --- | --- |
| 注册/更新实例 | registration_request_id | worker_instance_key 标识跨重启的实例 |
| 接收执行 | command_id | operation_id 标识跨 Execution 重试的逻辑副作用 |
| 恢复执行 | command_id | pause_id + approval_request_id 绑定暂停点 |
| 取消执行 | command_id | cancellation_epoch 标识取消栅栏 |
| 审批申请 | approval_request_id | pause_id 标识 Worker 的持久暂停点 |
| 提交结果 | result_id | execution_id + generation 标识本次尝试 |
| 上传资源 | URL 中的 resource_id | sha256 标识字节内容 |
| 确认执行停止 | stop_confirmation_id | cancel_command_id + execution_id + generation 幂等绑定停止事实 |

## 3. HTTP 接口

### 3.1 注册

**请求：** POST /api/worker-instances
**Body：** RegistrationRequest
**Header：** Authorization、X-Athena-Protocol-Version、Idempotency-Key = registration_request_id

body 必须包含持久 worker_instance_key、HTTPS origin 形式的 base_url（不带路径、查询或片段）、正整数 execution_timeout_seconds、max_concurrency 和 capability。服务端只连接部署允许列表中的地址，并在解析和连接时执行网络地址策略校验。实例 timeout 范围为 1–604800 秒；并发范围为 1–256。capability 恰含 can_do、cannot_do、input_schema、output_schema 四项。两份 schema 的根类型为 object，且注册时通过 Draft 2020-12 Meta-Schema 校验。

新注册返回 201 RegistrationResponse。精确重放返回 200 和原注册响应。通过相同 worker_instance_key 的新 registration_request_id 更新该实例；认证主体不匹配时返回 403。contract_revision 在能力内容变化时递增，不能改写旧版本。首次注册后实例保持不可分配，直到满足心跳恢复阈值。

错误状态：400 JSON 语法错误；401/403 身份失败；409 registration_request_id 内容冲突；422 REGISTRATION_INVALID 配置/契约无效；429/503 暂时服务过载。注册错误不交 Root 自动重规划。

### 3.2 任务接收

**请求：** POST /worker-api/v1/tasks
**Body：** TaskCommand
**Header：** Authorization（Athena Orchestrator 主体）、版本头、Idempotency-Key = command_id

TaskCommand 至少包含 run_id、plan_id、task_id、execution_id、generation、cancellation_epoch、operation_id、contract_id/revision、objective、acceptance_criteria、完整 input 和 execution_grant。execution_grant 绑定 Worker 实例、run/plan/task/execution 身份、generation、cancellation_epoch、过期时间及资源读写范围；HTTP 服务凭据仍负责认证。Worker 访问资源时将 grant.token 放入 X-Athena-Execution-Grant。

Orchestrator 在第一次 HTTP 发送前持久化首次发送时间和执行 deadline。命令投递的 HTTP 超时和重投都不能重置 deadline。Worker 对 command_id 和 body 摘要做持久去重，先保存收件记录再返回。输入按 contract_revision 对应的 input_schema 校验；无效输入返回 422 TASK_INPUT_INVALID，directive=root_replan。

新命令持久接收返回 202 CommandReceipt；相同命令重投返回 200，receipt_id 和 received_at 不变，disposition 可为 duplicate。相同 command_id 不同 body 返回 409 COMMAND_ID_CONFLICT。Worker 收件回执不是开始执行或完成执行证明。

### 3.3 审批申请和批次恢复

**申请：** POST /api/worker-approvals，Body ApprovalRequest，Idempotency-Key = approval_request_id。
**恢复：** POST /worker-api/v1/executions/{execution_id}/resume，Body ResumeCommand，路径 ID 必须等于 body execution_id，Idempotency-Key = command_id。

ApprovalRequest 包含当前 run/plan/task/execution、generation、cancellation_epoch、pause_id、approval_request_id 和 1–100 个 items。item_id 在请求内唯一；每项含 tool_name、summary、risk_level、完整 arguments 和 arguments_sha256。Worker 在发请求前必须已持久保存相同参数和暂停点。服务器先校验 Execution 仍运行且未超时/取消，再原子暂停该 Execution、为每个 item 创建独立 approval_id，并设置 expires_at = 首次服务端接收时间 + 120 秒。ApprovalReceipt 的 items 将 item_id、approval_id 和参数哈希一一对应；批次额外返回 approval_batch_id。申请的精确重放返回相同映射和 expires_at；不得重新通知或延长期限。

arguments_sha256 = SHA-256(JCS(arguments))。审批项由用户逐个决定。单项 ResumeCommand 只带 approval_id；批量 ResumeCommand 带 approval_batch_id，并且必须提交请求中每个 item 的最终决定，不能丢弃未通过项或仅提交已批准项。未到期的部分决定可先保存；全部决定完结，或等待满 120 秒时未决项目被标为 expired，服务端才生成一条完整 ResumeCommand。被取消的任务不生成恢复命令。

ResumeCommand 包含 command_id、execution_id、generation、cancellation_epoch、pause_id、approval_request_id、单项 approval_id 或批次 approval_batch_id、完整 decisions、decision_set_sha256 及执行授权。decision_set_sha256 = SHA-256(JCS(decisions))；决定数组按 item_id 的 UUID 字符串升序规范排列。每个 DecisionItem 必须携带原 item_id、decision、arguments_sha256 和 decided_at。Worker 校验集合成员与本地暂停快照完全相同后，才处理一次恢复命令。

通过和拒绝/过期决定都交 Worker。approved 只授权对应摘要的原始待审操作；denied 或 expired 不授权执行。Worker 可选合规替代方式或返回成功/失败，不由 Orchestrator 代替决策。部分批次不会部分恢复；Worker 收到完整集合后自主处理每项。

审批首次接收返回 202 ApprovalReceipt；精确重放返回 200，expires_at 不变。单项超过 1 项或批次 ID 与成员数不匹配、哈希不符时拒绝。暂停期间停止执行 deadline；决定产生后仍不开始执行计时，首次实际投递 ResumeCommand 时从持久 remaining_seconds 恢复。恢复等待上限独立于 120 秒审批时限。

### 3.4 取消

**请求：** POST /worker-api/v1/executions/{execution_id}/cancel
**Body：** CancelCommand；路径 ID 必须等于 body execution_id。
**Header：** Idempotency-Key = command_id。

CancelCommand 的 cancellation_epoch 对逻辑 task_cancelled/run_cancelled 至少为 1；execution_timed_out/execution_superseded 携带该 Task 当前 epoch，允许为 0。Worker 持久保存意图后总是返回 202 CancelReceipt，HTTP 重放永远返回原回执。

**停止确认：** POST /api/worker-executions/{execution_id}/stop-confirmation，Body StopConfirmation；路径 ID 必须等于 body execution_id；Idempotency-Key = stop_confirmation_id。Worker 在确认执行物理停止，或持久确认该代次已无法启动后调用。首次接受返回 200 StopConfirmationReceipt；同 ID/同摘要重放返回原 200 回执；同 ID/不同摘要或 cancel_command_id/执行代次不匹配返回 409 STOP_CONFIRMATION_CONFLICT。

Orchestrator 必须按 CancelCommand.reason 分开处理后再投递命令：`task_cancelled`/`run_cancelled` 表示逻辑取消，先在数据库原子地将 Task 标记为 `cancelled`、递增 `cancellation_epoch`，并使该 Task 的执行/恢复/重试命令失效；`execution_timed_out`/`execution_superseded` 只停止指定的旧 Execution，不得将仍需重试的逻辑 Task 标记为 `cancelled`，也不得递增其取消代次，只按该 Task 当前 `cancellation_epoch` 发送。Worker 按 reason 持久保存对应范围的停止意图：逻辑取消建立 Task 取消栅栏，超时/替代执行只封锁指定 Execution。逻辑取消先到时，后来到达的同代次任务和恢复命令不得启动；超时/替代执行先到时，旧 Execution 不得继续或启动，但 Task 的新代次仍可执行。

CancelCommand 对所有 reason 都只返回 202 CancelReceipt，含 accepted=true、reason 和 stop_confirmation_required=true；它只确认取消意图已持久保存，不能确认远程已经停止，也不释放容量。Worker 实际停止或持久确认该 Execution 已不会启动后，必须通过专用 StopConfirmation 接口回报；此规则同时适用于 task/run 逻辑取消、execution_timed_out 和 execution_superseded。Worker 先持久保存 StopConfirmation outbox，再投递给 Athena。

StopConfirmation 绑定原 cancel_command_id、execution_id、generation、cancellation_epoch 和 reason；Idempotency-Key = stop_confirmation_id。服务端必须验证该 CancelCommand 是由 Athena 发给该 Worker 实例且身份字段完全匹配。事务仅释放这个旧 Execution 的容量槽位一次，返回 StopConfirmationReceipt 的 capacity_released 或 already_released。该回执明确 task_status_unchanged=true：超时后的逻辑 Task 可以正在等待/运行新 Execution，旧执行停止回报不能取消 Task、改写结果、增加终态计数或唤醒 Root。重复 StopConfirmation 返回原回执。取消命令重放始终返回原 CancelReceipt；之后停止状态只由独立 StopConfirmation 接口表达。

ResultSubmission 仅接受 completed 和 failed。中心逻辑取消先提交时，迟到的成功/失败结果通过 ResultReceipt 的 ignored/task_cancelled 收口；它不作为物理停止证据。取消与成功结果在中心同一事务中竞争：先提交的一方决定逻辑 Task 是否取消/完成，状态不可倒退。

### 3.5 心跳

**请求：** GET /worker-api/v1/health
**Body：** 无；身份由 Worker 服务主体映射。
**成功响应：** HealthResponse，HTTP 200，必须回显本机注册时的 worker_instance_key。

status=ready 表示健康且 accepting_tasks 可独立为 true/false；status=not_ready 必须 accepting_tasks=false。注册中心收到响应后必须逐字比较 worker_instance_key 与当前探测的注册记录；缺少或不匹配时该次探测按失败处理，不得更新该实例的健康/恢复计数或重新加入可分配列表，并记录实例路由/代理配置错误。心跳只用于健康/接单判断，不携带执行 ID、进度或续租。中心达到连续失败阈值后移出可分配列表；达到连续成功阈值后才重新加入。单次健康失败不取消或重试已有 Execution。

同一服务 Bearer 主体可以拥有多个 worker_instance_key；健康判定归属 worker_instance_key，而不能只归属服务主体或 base_url。注册表中的期望 key 由 Athena 从认证主体下的实例记录取得，不能采用探测响应中的 key 反向选择目标实例。

### 3.6 结果提交

**请求：** POST /api/worker-executions/{execution_id}/result
**Body：** ResultSubmission；路径 ID 是执行身份，generation 必须等于被认证主体拥有的该 Execution 代次。
**Header：** Idempotency-Key = result_id。

Worker 先把结果和待提交状态持久落盘/入库，再提交。completed 必须有 output；failed 必须有 WorkerError。ResultSubmission 不包含 cancelled 状态；远程停止通过 3.4 节 StopConfirmation 提交。body 不能声明另一个 worker_instance_id。

有效成功结果需通过冻结 output_schema。输出无效时中心仍返回持久 200 ResultReceipt，disposition=recorded、reason=output_schema_invalid；任务记为失败并交 Root，不能再以相同 result_id 重试。执行失败按错误目录裁决：达到重试条件返回 disposition=retry_scheduled 和 next_execution_id；最终失败返回 recorded。取消、超时、旧代次的首次迟到结果返回 disposition=ignored 和对应 reason，不能更新 output、终态数或 Root 恢复作业。

相同 result_id 和摘要返回原 ResultReceipt，不重复转换状态或创建重试；相同 result_id 不同摘要返回 409 RESULT_ID_CONFLICT。结果回执是可靠提交的唯一确认；Worker 在获得回执前持续重投同一结果。Worker 侧结果 outbox 不设有限丢弃次数，间隔封顶后继续持久重试，直到获得 200 回执或被明确的 4xx 协议错误隔离。

### 3.7 大结果资源引用

ResourceReference body 有 resource_id、media_type、size_bytes、sha256 和可选 file_name。引用不包含本机路径，也不授予访问权。

TaskCommand/ResumeCommand 中的 execution_grant.token 是签名的执行授权；Worker 在每次资源 PUT/GET 时，同时发送服务 Authorization 和 X-Athena-Execution-Grant。授权至少绑定 worker_instance_key、run_id、plan_id、task_id、execution_id、generation、cancellation_epoch、过期时间及资源权限。写权限仅允许当前授权 Execution 创建资源；Athena 根据 grant 中的 execution scope 将资源元数据绑定到 Run/Plan/Task/Execution/Generation，客户端不能自行声明归属。读权限包括当前 Execution 自己创建的资源，以及 grant 显式列出的 read_resource_ids。Athena 只在被冻结的 Task.input 明确引用该 resource_id 且 Run/Task 授权有效时签发跨 Execution 读取权限；同一 Bearer 主体下的其他实例、其他 Run 或未列入 grant 的资源均返回 403。授权 token 过期或与注册实例不符也返回 403。token 不写入日志。

Worker 上传：PUT /api/worker-resources/{resource_id}，原始字节 body，Content-Type 为具体媒体类型，Content-SHA256 为 64 字符小写十六进制 SHA-256；Idempotency-Key 等于 resource_id。服务端先验证服务身份、Execution Grant、header 与实际 body 摘要，再依据 grant 记录资源归属；摘要不符返回 400。相同资源 ID 与相同摘要返回原 ResourceReference（201 首次，200 重放）；相同 ID、不同摘要返回 409 RESOURCE_ID_CONFLICT。读取：GET /api/worker-resources/{resource_id}，返回相同字节、Content-Type、Content-Length 和 Content-SHA256，并验证读 grant 明确包含该 ID。单资源上限 104857600 bytes；超限返回 413，Worker 将内容切成多个不可变资源并在其结果 JSON 中引用。资源引用的 sha256 和 Content-SHA256 都使用 64 字符小写十六进制。

## 4. 错误码目录和处置

### 4.1 Orchestrator 的 HTTP ErrorResponse

HTTP 错误体由 Athena 生成，directive 是中心裁决。实现以 code 和本表为准，不只看 4xx/5xx，也不把 Worker 自报 retryable 当授权。

| code | HTTP | directive | 固定处理 |
| --- | --- | --- | --- |
| AUTHENTICATION_FAILED | 401 | operator_action | 停止自动重试；运维修复服务身份/凭据。 |
| FORBIDDEN | 403 | operator_action | 停止自动重试；不交 Root 改写业务输入。 |
| PROTOCOL_VERSION_UNSUPPORTED | 426 | operator_action | 停止调用并升级兼容协议实现。 |
| MALFORMED_REQUEST | 400 | operator_action | JSON 无法解析、含重复 key 或 Header/路径结构不合法；修复客户端后再发新请求。 |
| RESOURCE_DIGEST_MISMATCH | 400 | operator_action | 上传声明摘要与实际字节不符；修复字节或摘要后使用新 resource_id。 |
| UNSUPPORTED_MEDIA_TYPE | 415 | operator_action | Content-Type 不符合 JSON/资源接口约定；修复客户端。 |
| PAYLOAD_TOO_LARGE | 413 | operator_action | JSON 或资源超出配置 body 上限；缩小或拆分数据。 |
| REGISTRATION_INVALID | 422 | operator_action | 修正实例配置/Schema；不得要求 Root 猜。 |
| VALIDATION_ERROR | 422 | operator_action | 通用 DTO/字段格式无效；只有 TASK_INPUT_INVALID 能据此触发 Root 重新规划。 |
| TASK_INPUT_INVALID | 422 | root_replan | 当前 Plan 输入无效；不重投同命令，等待本轮结束后交 Root 重新规划。 |
| APPROVAL_REQUEST_INVALID | 422 | operator_action | Worker DTO/参数快照违约；隔离请求并告警。 |
| UNKNOWN_CONTRACT | 422 | root_replan | 契约版本已不存在/不再可用；Root 重新选择能力。 |
| INSTANCE_NOT_READY | 503 | retry_same_command | 对原命令有界退避；不创建新 Execution。 |
| SERVER_OVERLOADED | 503 | retry_same_command | 依 Retry-After 同命令重投。 |
| RATE_LIMITED | 429 | retry_same_command | 依 Retry-After 同命令重投；继续遵守 deadline。 |
| TEMPORARY_UNAVAILABLE | 503 | retry_same_command | 网络/暂态服务故障，同一身份重试。 |
| IDEMPOTENCY_KEY_REUSED | 409 | operator_action | 同 key 不同 body；停投并报告协议冲突。 |
| COMMAND_ID_CONFLICT | 409 | operator_action | 不重试不同内容；保留原收件回执并报告。 |
| RESULT_ID_CONFLICT | 409 | operator_action | 不替换已持久结果；告警 Worker 数据损坏/错误复用。 |
| RESOURCE_ID_CONFLICT | 409 | operator_action | 不覆盖同 ID 下的不同字节；由 Worker 分配新资源 ID 或修复内容摘要。 |
| APPROVAL_ID_CONFLICT | 409 | operator_action | 不改写已有审批快照；隔离该请求。 |
| STOP_CONFIRMATION_CONFLICT | 409 | operator_action | 停止确认不匹配已持久 CancelCommand 或相同幂等 ID 内容冲突；保留占用并排查 Worker 收件记录。 |
| EXECUTION_NOT_FOUND | 404 | operator_action | 核对实例归属和持久状态；不创建新任务。 |
| EXECUTION_SUPERSEDED | 409 | stop | 丢弃本次旧代次命令；按已知中心状态收尾。 |
| EXECUTION_DEADLINE_EXCEEDED | 409 | retry_execution | 不继续该代次；由中心超时策略判定新尝试额度。 |
| TASK_CANCELLED | 409 | stop | 保存取消事实；停止继续该 Task 的工作与结果重试。 |
| APPROVAL_STALE | 409 | stop | 丢弃过期 pause/决定；不得恢复。 |
| WORKER_QUEUE_FULL | 429 | retry_same_command | 计划原子排队失败；在等待上限内重试相同提交身份。 |
| CAPABILITY_UNAVAILABLE | 422 | root_replan | 排队/恢复等待到期或无可用实例；将终态事实交 Root。 |
| INTERNAL_ERROR | 500 | retry_same_command | 命令/结果均保持相同 ID 重试；命令达到重投上限后仍等结果或执行 deadline。 |

### 4.2 Worker 提交的执行错误

WorkerError 字段为 code、message、retryable 和可选 details。Worker 可使用下列稳定代码；产品专用代码可使用同一正则命名空间，但须先登记目录。中心仅按目录匹配，未知错误即使 retryable=true 也不自动重试。

| WorkerError.code | 中心动作 | Root/运维去向 |
| --- | --- | --- |
| WORKER_UPSTREAM_TIMEOUT | 若执行次数未耗尽，创建新 Execution 并按执行退避排队 | 次数耗尽后交 Root |
| WORKER_RESOURCE_UNAVAILABLE | 若目录判定暂态且额度未耗尽，创建新 Execution | 次数耗尽后交 Root |
| WORKER_CAPACITY_LOST | 作为执行尝试失败；按额度重试 | 次数耗尽后交 Root |
| WORKER_INVALID_INPUT | 不重试当前业务；记录 Task 最终失败 | 本轮收集完交 Root 重新规划 |
| WORKER_UNSUPPORTED_OPERATION | 不重试；保留契约/实现不符证据 | 交 Root 重新规划；同时运维修正能力声明 |
| WORKER_OUTPUT_INVALID | 不重试；输出违反契约时本应由中心 schema 验证捕获 | 交 Root 重新规划，报告实现错误 |
| WORKER_PERMISSION_DENIED | 不重试，不把凭据问题交 Root 修改 | 交运维处理，并将执行失败事实提供 Root |
| WORKER_FATAL | 不自动重试 | 交 Root 处理最终失败 |
| 未登记代码 | 不自动重试，即便 retryable=true | 记录原始代码/原因并作为最终失败交 Root；若看似身份/配置问题，同时告警运维 |

Orchestrator 内部的 deadline 超时使用稳定错误 EXECUTION_DEADLINE_EXCEEDED，归入 retry_execution 目录。显式失败重试按 WorkerError 目录裁决；Worker 的 retryable 仅保留在结果事实中作为诊断提示。

### 4.3 重试边界

retry_same_command = 网络传输、HTTP 响应不明、限流或 Worker 已明确暂不可接收；命令 ID、Execution 和输入体不变，不重置执行截止时间。传输次数耗尽且可能已接收时只停止 HTTP 重发，继续等结果或原 deadline。

retry_execution = 中心创建新的 execution_id 和 generation；相同 Task/operation_id 和输入，执行尝试数加一，重新分配健康实例并冻结新 deadline。只有错误目录明确标记可重试且剩余额度足够才执行。

root_replan = 不重投坏请求；本轮其他任务继续直到终态，随后把错误及成功结果一起交 Root。Root 决定下一轮 Plan。

operator_action = 停止自动调用并发出结构化配置/身份/协议问题；不无限循环、不用模型修凭据。

stop = 命令/暂停点已失效或 Task 已取消；接受并保存忽略/停止回执，不启动新业务操作。

错误目录以外的未知 HTTP/Worker 错误默认“不自动做新业务执行”。对暂时传输错误可按 HTTP 状态重投同一个幂等操作；不得因为传输不可达而改成新 execution_id。

## 5. 配置表

以下值是协议 v1 默认值。后端配置由统一 settings 加载；Worker 结果 outbox 配置由 Worker 本地加载。秒数可为小数，计数为整数。配置持有方启动校验违反约束时启动失败，不静默钳制。

| 配置名 | 默认值 | 约束 | 语义 |
| --- | ---: | --- | --- |
| worker_dispatch_http_timeout_seconds | 5 | 0.1–30；必须小于 worker_heartbeat_interval_seconds | 单次执行/恢复/取消 HTTP 请求等待上限；超时意味着接收状态不明，仍重投同命令。 |
| worker_dispatch_max_concurrency | 4 | 1–64 | Athena 同时执行的 Worker HTTP 下发请求上限；独立于每个 Worker 的 max_concurrency。 |
| worker_dispatch_retry_initial_seconds | 1 | 0.1–60 | 命令传输初始退避。 |
| worker_dispatch_retry_max_seconds | 30 | initial ≤ value ≤ 300 | 命令传输退避封顶。 |
| worker_dispatch_max_retries | 8 | 0–100；是首次发送以外的重投次数 | 命令达到重投上限后不创建新 Execution；若曾可能接收，等结果/deadline。 |
| worker_dispatch_retry_jitter_ratio | 0.2 | 0–1 | 每次指数退避的随机抖动比例。 |
| worker_max_attempts | 3 | 1–10；包含首次执行 | 逻辑 Task 的总 Execution 次数；保留当前已有配置名与默认语义。 |
| worker_execution_retry_initial_seconds | 2 | 0.1–300 | 新 Execution 的初始排队退避。 |
| worker_execution_retry_max_seconds | 60 | initial ≤ value ≤ 3600 | 新 Execution 的重试退避封顶。 |
| worker_execution_retry_jitter_ratio | 0.2 | 0–1 | 新 Execution 退避随机抖动比例。 |
| worker_result_retry_initial_seconds | 1 | 0.1–60 | Worker 待提交结果的初始重投间隔。 |
| worker_result_retry_max_seconds | 60 | initial ≤ value ≤ 3600 | Worker 结果 outbox 退避封顶；没有有限重试次数，必须持续持久重投到收到 200 回执。 |
| worker_result_retry_jitter_ratio | 0.2 | 0–1 | Worker 结果重投的随机抖动比例。 |
| worker_heartbeat_interval_seconds | 15 | 1–300 | 健康探测周期。 |
| worker_heartbeat_request_timeout_seconds | 3 | 0.1–worker_heartbeat_interval_seconds | 单次心跳超时。 |
| worker_heartbeat_failure_threshold | 3 | 1–20 | 连续失败达到阈值即移出可分配列表；已有 Execution 不因此终止。 |
| worker_heartbeat_recovery_threshold | 2 | 1–20 | 连续成功达到阈值才重新可分配。 |
| worker_timeout_scan_interval_seconds | 1 | 0.1–60 | deadline、排队和恢复等待的扫描周期；时间比较用数据库时钟。 |
| worker_timeout_scan_batch_size | 100 | 1–10000 | 每个扫描批次上限；多个扫描器用持久租约/CAS 抢占。 |
| worker_queue_max_wait_seconds | 300 | 1–86400 | 已接受 Plan 中任务等待可用实例的最长时间；到期以 CAPABILITY_UNAVAILABLE 终结任务并交 Root。 |
| worker_queue_max_pending_tasks | 10000 | 1–1000000 | 全局持久待分配任务上限；新 Plan 超限时原子拒绝入队并返回 WORKER_QUEUE_FULL，不产生部分 Plan。 |
| worker_resume_dispatch_max_wait_seconds | 300 | 1–86400 | 用户/超时决定生成后等待绑定 Worker 有容量且可达的最长时间；到期以明确恢复投递失败交 Root，不迁移私有 Checkpoint。 |
| worker_approval_timeout_seconds | 120 | 固定；不可通过配置覆盖 | 服务端首次持久接收审批申请后设置的统一等待上限。 |
| worker_max_json_body_bytes | 1048576 | 1024–104857600 | 所有 Worker 协议 JSON 请求和响应的 body 上限，默认 1 MiB；超限返回 413。 |
| worker_resource_max_bytes | 104857600 | 1–1073741824 | 单个资源字节数上限，默认 100 MiB。 |
| worker_result_retention_seconds | 604800 | 60–31536000 | Worker 和中心保留结果/回执的最低时间；有关联 Task/Plan 未关闭时继续保留。 |

公式：某类重试第 n 次的 base 为 min(该类 max_seconds, 该类 initial_seconds × 2^n)，n 从 0 开始。实际 delay 从 [base × (1 - jitter_ratio), min(max_seconds, base × (1 + jitter_ratio))] 均匀抽取；Retry-After 存在时取 max(delay, Retry-After)。若等待会超过对应 deadline/排队上限，则不发送会失效的重试。

实例注册字段 execution_timeout_seconds 范围为 1–604800，max_concurrency 为 1–256；它们由 Worker 实例配置给出，不是后端的全局默认值。首次发送开始执行时冻结 timeout。排队等待不消耗该执行时长；审批暂停保存 remaining_seconds，恢复实际发送前才从剩余额度继续计时。

排队顺序为持久 FIFO：eligible_at、created_at、task_id 依次排序。实例有容量时取最早任务；达到全局排队上限时新 Plan 全有或全无地拒绝；单任务排队到期后进入终态失败，不能无界滞留。恢复队列也按决定持久时间 FIFO，但始终绑定原 Worker 实例。

## 6. JSON Schema 样例和验证

worker-protocol-v1.examples.json 覆盖注册请求/响应、执行和接收回执、心跳、逻辑取消和超时取消、独立停止确认、单/批审批体与恢复、成功/失败/未知错误结果、结果重放、资源引用和 HTTP 错误示例。正例 payload 映射到共享 schema 的 $defs；反例必须由 Schema 拒绝；lifecycle_scenarios 验证跨请求身份、范围和状态关系。

规范 JSON 校验使用 Python jsonschema Draft 2020-12 Validator，并启用 FormatChecker。批量审批示例额外按 RFC 8785 JCS 核对每个 arguments_sha256 及 decision_set_sha256；幂等头/body 匹配、路径绑定、心跳实例键、资源 Grant 范围和当前代次属于服务端业务校验。新增正例必须通过对应 body schema；反例必须至少违反所指 schema 或明确的跨请求不变量，并在 validator 中断言其被拒绝。

## 7. 阶段验收边界

S1 已冻结 DTO、路径、HTTP 状态、幂等键、错误处理和运行配置默认值。S1 不代表后端路由、数据库模型、后台扫描器或独立 Worker 已实现。S2 起双方按本协议实施；遇到需要变更字段、错误动作、幂等作用域或默认配置时，先更新协议 Schema 和样例并升协议版本或明确兼容策略，再实现变更。

