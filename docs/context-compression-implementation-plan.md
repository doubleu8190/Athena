# LLM 上下文压缩实施方案

状态（2026-11-13）：本方案已完成实施，提交前独立审查 PASS，无有效 Finding；safe-int P2 与安全报告事务 P1 均已关闭，前端 Reviewer 再次 PASS，Root 最终验收通过。P1 修复采用同一完整 `FailRunCommand` 在有效执行权内最多三次幂等重放，专用 typed 异常穿透 execute，Worker 禁止 generic failed 覆盖，并以既有 typed usage 命令尽力保留调用审计。最终证据为后端单元 `907 passed, 1 skipped`、六个 PG 集成文件 `12 passed`，Ruff、`mypy backend/src`、前端 `277 passed`/build/浏览器 e2e `5 passed` 均通过，`backend/src backend/tests` 仅余 9 个既有 typing 基线错误。真实外部 LLM 质量尚未联调；提交状态以 Git 记录为准，本轮未 push 或 deploy，`0024` 未应用到在线数据库。2026-11-09 的历史结果见文末，不作为本轮证据。

## 1. 交付目标

在现有 Run、Worker、Chat Completions Adapter、PostgreSQL、Redis 事件和 SSE 结构内完成以下闭环：

- 常规压缩使用管理员配置的绝对阈值 `T`，默认 `262144`，不再乘 80%。
- 近期原文使用 `min(T * recent_ratio, recent_token_cap, 模型可行预算)`，默认 `recent_ratio=0.2`、`recent_token_cap=32768`。
- 摘要使用 `min(T * summary_ratio, summary_token_cap, 模型可行预算)`，默认 `summary_ratio=0.1`、`summary_token_cap=8192`。
- 每次请求准备最多一次摘要 Provider 调用，不拆批、不截断、不切模型、不自动重试。
- 完整业务输入达到 `0.9W` 或违反 `I + O <= W` 时停止业务模型、工具和恢复批次。
- 无 tools 报告或系统报告与 `FAILED` 终态原子、幂等保存，不新增 Run 状态。
- Run API、SSE 快照和 failed 事件暴露持久化报告引用，供前端精确对账。

产品和业务行为以 [上下文压缩设计](context-compression-design.md) 第 3 节为准。

## 2. 实施基线与本轮结果

| 位置 | 修订前事实 | 本轮实际结果 |
| --- | --- | --- |
| `backend/src/app/run/compression.py` | 以 `input_budget * trigger_ratio` 触发；分批循环；摘要最多三次尝试和退避；无新历史也可能重整超限摘要 | 已改为完整业务 `I` 与绝对 `T` 比较；生产路径只做一次摘要请求；删除摘要分批、自动重试和无新历史重整；旧摘要超 cap 继续复用；失败事件记录候选身份并在后续 `prepare` 去重 |
| `backend/src/app/run/executor.py` | 先恢复工具批次；无策略、`final_only`、槽位不足直接构建；压缩异常后外层回装全历史 | 每轮先执行上下文准备和安全裁决，再决定恢复批次或模型调用；外层全历史回装已移除；安全收尾命令一次构造，在有效执行权内最多三次同命令幂等提交，专用异常穿透 execute |
| `backend/src/app/run/budget.py` | 压缩和回答共享 Run 请求计数，保留最后无工具回答槽位 | 保持原计数机制；报告也受总上限和 deadline 约束，无槽位走系统报告 |
| `backend/src/app/llm/profile_service.py` | 策略用户用例实际由 `ModelProfileService` 管理 | `ModelProfileService` 已接入五字段严格校验和不可变策略保存 |
| `backend/src/domain/llm/model/compression_policy.py` | 三个比例字段 | 已改为 `trigger_tokens`、双比例、双 Token cap；`K=1024` 建议值已落地；三个 Token 字段统一限制为 `1..MAX_COMPRESSION_TOKEN_BUDGET`，MAX 为 `2**53-1` |
| `backend/src/adapter/run/repository/run.py` | `fail` 只写失败状态和事件；`finish` 会先写 completed | `fail` 已支持报告消息、FAILED、usage、事件和未完成工具收敛的同事务提交；同一稳定报告命令重放返回既有终态，安全停止不复用 completed |
| `backend/src/domain/run/model/entities.py`、`stream.py`、Run API | 无报告引用 | Run 和快照增加 `report_message_id: str \| null`；failed 事件可选携带 `report_message_id`、`context_stop_reason` |
| `frontend/src/features/chat/SessionChatPage.tsx` | `failed` 与 `cancelled` 一起早退，只有 completed 同步最终回复 | 已将 failed 报告同步与 cancelled 早退分开，并校验目标 Run、assistant、completed 和 reply-to trigger；前端独立复核与 Reviewer 均通过 |
| `backend/migrations/versions` | head 原为 `20261122_0023` | 新 head 为 `20261123_0024`，没有修改 `20261112_0013` |

工作树包含前端 Implementer 和其他任务的改动。本实现只修改 `backend/**` 与本文件、设计文档，不覆盖前端、知识模块或其他 untracked 内容。

## 3. 已确认规则

### 3.1 完整输入与固定硬窗

- `I` 包含 system prompt、tools、协议、摘要、未覆盖历史、当前 Run 必要交互、文件、Skill 和重试/恢复上下文。
- `O` 单独预留；所有业务和报告请求都必须满足 `I + O <= W`。
- 90% 分子只使用 `I`，不使用 `I + O`。
- 业务裁决使用原业务 tools；不能先清空 tools 降低输入绕过 90%。
- 报告使用独立 `I_report`，允许 `I_report >= 0.9W`，只要求 `I_report + O_report <= W`。

### 3.2 触发、预算和摘要复用

- 常规触发为 `I >= T`，默认建议 `T=262144`、`K=1024`。
- `T` 高于 `0.9W` 或小模型不能绕过 90% 停止线。
- `0.9W <= I < T` 时，允许实现一次提前救援压缩；这不改变 256K 常规触发规则。
- `recent_ratio`、`summary_ratio` 继续由管理员配置，默认 20%/10%；两个比例之和必须小于 1。
- 当前 Run 全保护；只压缩触发前、旧摘要边界后的连续历史；无新历史 skip。
- 旧摘要即使超过新 `summary_token_cap` 也完整复用。无新历史不重整、不回装；有新历史才生成满足新 cap 的增量版本。
- 失败候选以 base summary ID 和候选覆盖终点持久标识。同终点或收缩候选 skip；只有覆盖终点连续前移才允许新尝试。当前 Run Execution、工具结果和 Skill 提示增长不属于旧历史扩展。

### 3.3 单次摘要调用

- 旧摘要、全部尚未覆盖的候选历史、压缩指令和输出预留一次装入压缩模型。
- 输入装不下：0 Provider 调用、0 Run 槽位、无 usage、不拆批/截断/切模型。
- 可以调用时只调用一次；超时、网络错误、非法响应或输出超限不重试。
- Provider 和 SDK 不隐藏 retry；现有 `AsyncOpenAI(max_retries=0)` 保持。
- 生成失败保留旧摘要和原文，无部分版本；只在单次成功、双 Tokenizer 校验和硬窗检查通过后原子保存。
- 压缩失败低于 90% 且兼容快照满足硬窗时继续；executor 不重新回装全历史。
- 首次 Provider 调用前的输入装不下、模型不可用和预算失败也写带候选身份的失败事件。下一次 `prepare` 通过现有 `RunRepository.list_events` 恢复最近失败；旧事件无候选字段时视为未知，不新增表或兼容迁移。
- 失败候选 skip 固定为 0 Provider 调用、0 Run 槽位、0 usage、0 新增压缩事件，保留上次真实失败原因；后续仍执行完整 90%/硬窗裁决。同候选失败判断先于 `allow_compression` 判断，因此业务耗尽最后槽位时也保留原失败原因；只有真实扩展后的新候选无槽位时才使用 `compression_budget_exhausted`。
- Run 最新 failed 重试、数据库幂等重放和主回答应用级重试不属于摘要重试。

### 3.4 90% 安全停机

每次业务主模型调用和 `resumable batch` 副作用前执行：

1. 读取当前持久化上下文、原业务 tools、文件和 Skill。
2. 按常规阈值或一次提前救援机会尝试合法历史压缩。
3. 对最终候选重新计算完整 `I` 和 `O`。
4. `I >= 0.9W` 或 `I + O > W` 时停止业务模型、工具和未执行恢复批次。
5. 通过后才允许业务模型或持久化工具批次继续。

`final_only`、无策略、无压缩槽位和恢复批次都不能短路。停止需收敛未派发/`unknown executing` 工具，协议闭合，不误报为已执行。

### 3.5 失败报告与状态

- 安全码：`run_context_safety_stop`。
- 受控原因字段：`context_stop_reason`。
- 报告模型请求无 tools；输出必须包含“已完成、产出、未完成、停止原因”四部分，缺少结构时使用系统报告。
- 报告可以只使用持久化的必要状态，不能截断当前 Run 的必要交互；完整必要材料装不下时直接系统报告。
- 报告失败、无槽位、deadline 无剩余或输入不能安全入窗时仍尝试提交确定性系统报告；取消、失租、鉴权和 deadline 优先。
- 报告消息与 Run `FAILED`、安全码、usage、报告 ID、failed 事件和工具协议收敛在同一事务中提交。
- 压缩失败与 `I + O > W` 同时发生时，对外 `context_stop_reason` 保持既有 `hard_window_exceeded` 优先级；内部报告状态和 canonical“停止原因”同时记录真实压缩失败类别，报告模型不能省略。
- 报告必须是目标 Run/Session/owner 的 `assistant`、`completed` 消息，reply/current turn 指向触发消息，sequence 晚于触发。
- 同稳定报告 ID 和安全码的重放返回既有终态，不重复写消息、usage 或事件。
- 不使用 `finish` 写 completed 后再 fail；不新增 Run 状态。

### 3.6 前后端契约

已经固定并由后端投影：

- `RunResponse.report_message_id: str | null`
- SSE snapshot `payload.report_message_id: str | null`
- `run.failed` 可选 `report_message_id: str | null`
- `run.failed` 可选 `context_stop_reason: str | null`

前端按 Run 或 failed 事件的报告 ID 定位权威报告；不得退回最新 assistant 消息。`SessionChatPage` 必须为 failed 增加报告同步，cancelled 保持无报告语义。

### 3.7 既有不变量

单次摘要简化不删除以下已确认要求：

- 增量摘要顺序连续、覆盖不跳段、父摘要同 Session/owner，覆盖终点严格早于触发。
- 摘要请求先完成完整输入和独立输出预留检查；业务最终请求也完整检查。
- 摘要正文完整、有效、非截断且无 tools；禁止硬截字符串。
- 压缩请求使用压缩模型实际 Tokenizer，最终摘要使用主模型实际 Tokenizer。
- 生成过程中不写中间摘要或部分版本；全部成功后才提交最终版本与完成事件。
- 最终保存和完成事件同事务，按 Session → Run 锁顺序复核 owner、Session、running、取消、worker/epoch、租约和 deadline。
- 操作 ID、数据库幂等锁、取消/失租/session-owner 校验保留。
- 仅最新 failed Run 可按 `created_at DESC, id DESC` 重试，复用源触发和 cutoff。
- usage 按 purpose、调用、重试和执行代次独立记录；未知不填 0。
- 业务工具批次、`resumable batch` 和协议闭合继续保留。
- `RunCompressionFailedPayload` 新增可选 `candidate_base_summary_id`、`candidate_covered_through_sequence`；读取本 Run 最近 typed `compression_failed` 事件即可恢复，不引入新表、迁移、通用状态机或前端字段。

## 4. 实施记录

### S0：规则冻结与矩阵

已完成：绝对阈值、双比例、双 cap、单次摘要、90% 减停和报告原子性测试矩阵均转入本轮测试。

### S1：策略域、API 与迁移

已完成：

- 五字段 typed 输入和不可变版本。
- `ModelProfileService` 管理员鉴权、严格类型/范围校验和不可变保存。
- `GET/PUT /api/v1/admin/llm/compression-policy` 改用五字段；`trigger_ratio` 不再属于 API 契约。
- `trigger_tokens`、`recent_token_cap`、`summary_token_cap` 在 DTO、service 和 repo 写边界统一校验 `1 <= value <= 2**53-1`；越界 API 输入返回 422，不能进入 PostgreSQL 后形成 500。
- 策略模型增加可为空 Token 字段；历史 `trigger_ratio` 列保留，新实例只读取完整新格式三条 Token 字段。
- 迁移从 `20261122_0023` 推进到 `20261123_0024`，增加新字段、正向约束和 `runs.report_message_id` 外键。
- 既有新格式字段不完整的策略投影为不可用，不把旧 float 不静默换算。

未包含：比例策略的兼容、回填或自动转换。该范围不阻塞本轮，迁移保证现有 DB 可升级且旧引用不丢失。

### S2：预算、候选历史与单次摘要

已完成：

- `measure_llm_request` 返回完整输入、原业务 tools 和输出预留。
- 触发、近期 suffix 和摘要预算按绝对 `T` 与 cap 计算。
- 生产压缩路径只保留 `_compress_single`；摘要批次方法和批次循环不再存在。
- `_compress_single` 先计算完整压缩请求，预检通过后才预占 Run 槽位和调用 Provider。
- 摘要路径显式维护派发状态。失败事件区分 `batch_count=0` 的预检失败和 `batch_count=1` 的已派发失败；成功 usage 固定 `batch_index=0`、`attempt=1`。
- 所有压缩失败事件在 Provider 分支之前即携带候选 base summary ID 和候选终点。后续 `prepare` 若候选终点相同或收缩，直接以兼容快照返回；仅终点真实前移时继续单次尝试。
- 相同候选判断通过分页读取本 Run 的 `compression_failed` 事件恢复，不依赖 `ContextCompressor` 内存；Worker 重建、审批恢复和历史策略恢复后语义一致。
- Provider 返回后的输出 Tokenizer 或最终主上下文测量若抛出普通 `RunContextError`，仍保留一次 Provider 调用、一个 Run 槽位和已知 usage；取消、失租与 execution deadline 不进入普通失败回退。
- 成功摘要同时以压缩模型和主模型 Tokenizer 校验，再原子保存。
- 无新历史、旧摘要超 cap、窗口变大和增量边界行为均使用目标规则。

### S3：Run 编排与 90% 闸门

已完成：

- `_run_loop` 先读取 `resumable batch`，但在执行副作用前调用 `_prepare_request` 做安全裁决。
- 普通、`final_only`、无策略、无压缩槽位和恢复批次都使用完整业务 tools 的 `I`。
- 只有安全裁决通过后才移除 final-only tools 或执行恢复批次。
- 90% 取整使用向上取整；`I >= ceil(0.9W)` 停止。
- `I < 90%` 但 `I + O > W` 也停止。
- 压缩异常由压缩层返回兼容快照；executor 外层全历史回装已删除。
- 提前救援压缩只在 `0.9W <= I < T`、有新历史且有槽位时发生一次。
- 失败候选 skip 后仍使用原业务 tools 的完整 `I` 执行 90% 与硬窗检查，不会因未再次调用压缩而绕过停止线。

### S4：报告与原子失败终态

已完成：

- 安全停止先做租约/取消/鉴权检查，再尝试一次无 tools 报告。
- 报告请求单独做原业务窗口硬窗检查，不使用 90% 业务线。
- 报告状态包含当前 Run 全部文本、工具调用与结果、合法文件引用、输入文件、完整已加载 Skill 上下文、安全可见 artifact 和 retry 上下文；不截断，装不下时零报告模型调用并直接系统兜底。
- 硬窗覆盖外部停止原因时，报告状态另带真实压缩失败类别；系统报告和 canonical 事实校验同时保留硬窗与压缩结论。
- 平台先渲染固定四节 canonical 系统报告。模型只能在章节内重排逐字不变的事实行；集合或多重集不一致、未知 file ID、错误完成状态或遗漏持久记录均回退系统报告。
- 系统兜底只展示安全元数据，但保留文件、输入文件、Skill、retry、artifact、工具状态和准确停止原因，不粘贴完整超大正文。
- 报告调用完成后再次复核租约/取消与鉴权；授权被撤销时按 `run_auth_session_invalid` 普通收敛，不写报告、报告 ID 或安全 failed payload，但保留已经发生的报告模型槽位和 known/unknown usage。
- `FailRunCommand` 扩展报告、usage 和原因；`fail` 同事务写报告、FAILED、安全码、usage、failed 事件并收敛未完成工具。
- 同一报告 ID 的并发幂等重放先于租约清理判断，避免误报失租。
- 取消竞争仍优先；deadline 已失效时不补写安全报告。

### S5：管理员前端契约

已完成。前端按后端固定五字段 API 实施并通过独立复核：

- 仅删除 `trigger_ratio` 输入/类型/请求。
- 保留五字段编辑、比例与正整数约束。
- 派生预算基于实际管理比例计算：`min(T * recent_ratio, recent_token_cap)` 和 `min(T * summary_ratio, summary_token_cap)`。
- 覆盖未配置、Token 边界、比例和约束、停用模型、不可变保存和错误提示。

### S6：聊天失败报告同步

已完成。`failed` 报告同步已位于权威最终消息分支，`cancelled` 保持早退。失败报告必须位于 trigger 之后；前端校验目标 `run_id`、assistant、completed 和 reply-to trigger，拒绝 trigger 前或错误 Run 的消息。SSE 重连/刷新后通过 Run `report_message_id` 或 failed 事件恢复，不依赖页面本地瞬时状态。文件刷新只表示文件记录变化。

## 5. 实际改动范围

后端源码和迁移：

- `backend/src/domain/llm/model/compression_policy.py`
- `backend/src/adapter/llm/model/profile.py`
- `backend/src/adapter/llm/repository/profile.py`
- `backend/src/app/llm/profile_service.py`
- `backend/src/api/dto/llm.py`
- `backend/src/api/converter/llm.py`
- `backend/src/api/llm.py`
- `backend/src/domain/run/model/entities.py`
- `backend/src/domain/run/model/stream.py`
- `backend/src/domain/run/model/__init__.py`
- `backend/src/adapter/run/model/run.py`
- `backend/src/adapter/run/converter/run.py`
- `backend/src/adapter/run/repository/run.py`
- `backend/src/api/dto/run.py`
- `backend/src/api/converter/run.py`
- `backend/src/app/run/stream.py`
- `backend/src/app/run/context.py`
- `backend/src/app/run/compression.py`
- `backend/src/app/run/executor.py`
- `backend/migrations/versions/20261123_0024_add_context_safety_and_absolute_compression.py`

后端测试：

- `backend/tests/test_run_compression.py`
- `backend/tests/test_run_executor_tools.py`
- `backend/tests/test_llm_profile_service.py`
- `backend/tests/test_run_worker.py`
- `backend/tests/test_runs_api.py`
- `backend/tests/test_run_stream.py`
- `backend/tests/integration/test_context_compression_postgres.py`
- `backend/tests/integration/test_context_compression_e2e_postgres.py`
- `backend/tests/integration/test_context_compression_race_postgres.py`
- `backend/tests/integration/test_llm_profile_postgres.py`
- `backend/tests/integration/test_system_prompt_postgres.py`

文档：

- `docs/context-compression-design.md`
- `docs/context-compression-implementation-plan.md`

前端文件和知识模块 untracked 属于其他 Implementer，不在本清单或本实现 authorized scope 内。

## 6. 测试矩阵与实际结果

| 测试组 | 覆盖行为 | 本轮结果 |
| --- | --- | --- |
| 策略与 API | 五字段、默认值、权限、不可变版本、旧策略不被静默换算、三 Token 字段 safe-int 边界 | 通过；safe-int P2 定向回归已完成，并由 fresh Reviewer 确认 |
| 上下文预算 | 完整 `I`、原业务 tools、独立 `O`、当前 Run、文件/Skill/retry | 通过 |
| 摘要编排 | 无新历史 skip、旧摘要超 cap 复用/增量、单次调用、拆批删除、双 Tokenizer、原子保存 | 通过 |
| 失败候选去重 | 同候选跨 `prepare`/Worker 重建 skip、仅当前 Run Execution 不扩展、预检/模型不可用/Provider 失败、候选终点扩展重试、失败原因保留 | 通过，后端 Reviewer 第 2 轮 PASS |
| 失败与 usage | 预检 0 调用/0 槽位、无伪 usage、派发一次、无自动重试、未知 usage、后置校验失败保持 1 调用/1 槽位/已知 usage | 通过 |
| 90% 与硬窗 | final-only、无策略、无槽位、恢复批次前停止、提前救援、报告硬窗 | 通过 |
| 报告原子性 | canonical 事实行校验、未知信息/错误完成/遗漏记录拒绝、系统兜底、后置鉴权撤销、报告与 FAILED 原子、幂等、取消竞争、写失败回滚、同命令有界重放、typed 异常穿透与 Worker 禁止 generic 覆盖 | 通过；fresh Reviewer P1 复审 PASS，定向测试 `5 passed` |
| 迁移 | `0023 → 0024` 升级、旧策略/Run/current 引用保留、head 唯一 | 通过 |
| SSE/API | Run 报告 ID、snapshot 报告 ID、failed 事件字段、失败重连单元 | 通过 |
| 安全停机全链路 e2e | HTTP→Worker→PG→Redis→SSE、丢失通知/重连、FAILED 报告 | 通过：新增 1 个集成用例 |
| 前端 | 管理五字段和 failed 报告同步 | 本轮未改；Root 复核仍为 `277 passed`、build 通过、浏览器 admin e2e `5 passed`，独立 Reviewer PASS |

实际命令与结果：

- 第 2 轮返修定点：
  - `.venv/bin/pytest -q backend/tests/test_run_compression.py`：`25 passed`。
  - `.venv/bin/pytest -q backend/tests/test_run_executor_tools.py -k 'hard_window_stop_preserves_compression_failure_reason or context_safety_gate_covers_final_only_without_compression_policy'`：`3 passed, 38 deselected`。
- 提交前 safe-int P2 定向回归：
  - `.venv/bin/pytest -q backend/tests/test_llm_profile_service.py`：`24 passed`，覆盖三字段 MAX 接受、MAX+1/`2**80` 拒绝以及 HTTP 422。
  - `source scripts/load-test-env.sh` 后运行 `backend/tests/integration/test_llm_profile_postgres.py`：`1 passed, 1 warning`，验证 MAX 可持久化、越界值不新增策略行。
  - `.venv/bin/mypy backend/src`：305 个文件无错误；相关 Ruff 定向检查通过。
- 提交前报告提交 P1 定向回归：
  - `.venv/bin/pytest -q backend/tests/test_run_executor_tools.py backend/tests/test_run_worker.py`：`56 passed`。
  - 覆盖 execute 级首事务失败后同命令重放、持续失败 typed 穿透、Worker 不写 generic failed、失租/仓储拒写停止重放、已知 usage 审计以及鉴权和取消优先路径。
  - `source scripts/load-test-env.sh` 后运行报告原子性 PG 用例：`2 passed, 2 warnings`；验证整体回滚、幂等与取消竞争。
  - 安全 E2E owner 将真实 PG 用例参数化为 normal/report-event-write-retry 后两例通过；报告消息 flush 后 failed event INSERT 故障一次，完整 `FailRunCommand` 重放字段一致，最终一份 message/event、一个 LLM slot 和一次 usage attempt。原六文件合计更新为 `12 passed`。
- 前轮 `.venv/bin/pytest -q -m 'not integration and not llm_live and not real_ocr'`：
  - `893 passed, 1 skipped, 52 deselected, 5 warnings`，耗时 `39.44s`；这是 safe-int/P1 修正前的主体记录。
- P1 修复前的中间单元记录为 `903 passed, 1 skipped`；Root 在 P1 修复后的当前最终覆盖为：
  - `.venv/bin/pytest -q -m 'not integration and not llm_live and not real_ocr'`：`907 passed, 1 skipped, 53 deselected, 5 warnings`，耗时 `37.33s`。
- 前轮 `source scripts/load-test-env.sh` 后运行压缩相关的 PostgreSQL/Redis/SSE 集成文件：
  - 六个文件：`11 passed, 14 warnings`，耗时 `19.92s`。
  - 新增 `test_context_safety_e2e_postgres.py::test_context_safety_public_run_worker_pg_redis_sse_recovery` 覆盖 finding 4 的真实安全停机全链路。
  - 使用随机 schema；只删除测试创建的 schema，未 flush Redis 全库、未 drop 共享 DB、未修改 dev 数据。
- P1 修复后的当前最终集成回归：
  - 六个 PostgreSQL/Redis/SSE 集成文件：`12 passed, 15 warnings`，耗时 `30.93s`。
  - 覆盖 normal 与 report-event-write-retry 两例真实 PG 安全收尾闭环。
- `.venv/bin/alembic -c backend/alembic.ini heads`
  - `20261123_0024 (head)`。
- `.venv/bin/ruff check backend/src backend/tests`
  - 通过。
- 新增格式检查 5 个文件：`backend/src/adapter/llm/repository/profile.py`、`backend/src/app/run/compression.py`、`backend/tests/test_run_stream.py`、`backend/src/domain/run/model/entities.py`、`backend/tests/integration/test_context_safety_e2e_postgres.py`
  - 全部通过。
- `.venv/bin/mypy backend/src`
  - 305 个文件无错误。
- `.venv/bin/mypy backend/src backend/tests`
  - 只余 9 个既有无关错误；新增安全停机 e2e 的类型问题已由文件 owner 修复，没有新增错误。
  - 本实现自身文件没有新增 mypy 错误。
  - Root 基线原为 17 个错误、6 个文件；本轮顺手修复了受影响 `test_llm_profile_service.py` 中的 8 个 DTO 输入类型错误。
- `git diff --check`
  - 通过。
- P1 修复后最终静态复核：
  - `ruff check backend/src backend/tests`：通过。
  - `.venv/bin/mypy backend/src`：305 个文件无错误。
  - `git diff --check`：通过。
  - 前端未修改，仍为 `277 passed`、build 通过、浏览器 admin e2e `5 passed`。

未运行真实外部模型质量联调；确定性 Provider 测试不能替代真实供应商兼容性和摘要质量证据。

## 7. 主体验收、提交前修正与提交状态

2026-11-13，提交前独立审查 PASS，无有效 Finding；safe-int P2 与安全报告事务 P1 均关闭，前后端 Reviewer 均 PASS。Root 已对冻结后的最终代码完成验收。

两轮返修内容：

- 第一轮：安全报告写前再次鉴权；报告状态保留完整当前 Run 文件、Skill、retry 与工具材料并执行 canonical 事实行校验；Provider 已派发后的后置校验异常保持 1 次调用、1 个槽位和已知 usage；补充真实 HTTP→Worker→PG→Redis→SSE 安全停机回归。
- 第二轮：压缩失败事件持久化候选 base summary ID 与覆盖终点；同候选、收缩候选及仅增加当前 Run Execution 的情况直接 skip，只有连续旧历史覆盖真实前移才允许新尝试；最后业务槽位与硬窗收尾继续保留真实压缩失败原因。
- 提交前复核：压缩策略三个 Token 字段统一 `1..2**53-1` safe-int 上界；DTO、service 和 repo 均拒绝越界值，避免 API 500 与 PostgreSQL 不可写值。
- fresh Reviewer 新 P1：安全收尾报告命令异常曾被吞掉并在 execute/Worker 二次降级为无报告普通失败。现改为同一完整命令最多三次幂等提交，新增 `RunContextSafetyCommitError` 穿透 execute，Worker 对 `RUNNING` 行禁止 generic 覆盖；取消、失租、deadline、鉴权或领域状态拒绝优先；已知 usage 以既有 typed 命令尽力审计。
- P1 复审：fresh Reviewer PASS，无有效 Finding，定向测试 `5 passed`；P2 与 P1 全部关闭，前端 Reviewer 再次 PASS，Root 对冻结代码完成最终验收。

主体最终验收证据（P2 修正前）：

- 旧摘要超 cap、增量新上限和 90% 停止均有实现测试。
- 摘要路径无生产分批、中间串联和自动重试；预检/派发失败语义已区分。
- 所有业务路径都执行 90% 与硬窗口裁决；报告只执行独立硬窗口检查。
- 报告与失败终态原子、幂等，Run API/快照/failed 事件可定位。
- PostgreSQL 升级、并发竞争、报告写失败回滚及压缩/安全停机的 Redis/SSE 闭环已实际通过。
- 另一 Implementer 交付的 finding 4 HTTP→Worker→PG→Redis→SSE 安全停机 e2e 已独立运行通过。
- 前轮后端单元：`893 passed, 1 skipped, 52 deselected, 5 warnings`；safe-int 后、P1 修复前中间记录为 `903 passed, 1 skipped`。
- P1 修复后的当前最终后端单元：`907 passed, 1 skipped, 53 deselected, 5 warnings`，耗时 `37.33s`。
- 前轮 PostgreSQL/Redis/SSE：六个集成文件 `11 passed, 14 warnings`；P1 修复后的当前最终回归为 `12 passed, 15 warnings`，耗时 `30.93s`。
- 前端：`277 passed`、build 通过、浏览器 admin e2e `5 passed`。
- Ruff 与指定 5 文件格式检查通过；`mypy backend/src` 305 个文件通过。

提交前修正定向证据：

- `backend/tests/test_llm_profile_service.py`：`24 passed`；覆盖三字段 MAX 接受、MAX+1/`2**80` DTO 与 service 拒绝以及 HTTP 422。
- PostgreSQL `backend/tests/integration/test_llm_profile_postgres.py`：`1 passed, 1 warning`；验证 MAX 可持久化、越界值不新增策略行。
- `backend/tests/test_run_executor_tools.py backend/tests/test_run_worker.py`：`56 passed`；覆盖同命令重放、持续失败 typed 穿透、Worker 禁止 generic 覆盖、失租/仓储拒写停止重放、usage 审计及鉴权/取消优先路径。
- 报告原子性 PostgreSQL 定向用例：`2 passed, 2 warnings`；安全 E2E normal/report-event-write-retry 两例真实 PG 通过，六个集成文件当前合计 `12 passed`。
- `mypy backend/src`：305 个文件无错误；相关 Ruff 定向检查通过；`git diff --check` 通过。
- safe-int P2 与报告提交 P1 均已由 fresh Reviewer 复审 PASS；Root 最终复跑和验收已完成。提交状态以 Git 记录为准，本轮未 push 或 deploy，`0024` 未应用到在线数据库。

明确限制：

- 未运行真实外部 LLM 质量联调，确定性 Provider 结果不能证明真实供应商的摘要质量。
- `mypy backend/src backend/tests` 仍有 9 个既有无关测试 typing 基线错误，本轮不扩修。
- 修改范围外的旧 format 差异未整体重排；只保证本轮新增格式的 5 个文件通过。
- 既有比例型策略的兼容、回填或自动转换不在本轮范围。
- 提交状态以 Git 记录为准；本轮未 push 或 deploy，`0024` 未应用到在线数据库。

## 8. 历史实施记录（2026-11-09）

旧版方案曾按 80%/20%/10% 比例触发，并采用分批摘要与每批重试。当时后端历史全量结果为 `516 passed, 3 skipped`，另完成 Ruff、mypy、格式检查和 PostgreSQL/Redis 事务竞争测试；独立 Reviewer 第 2 轮为 PASS，Root 完成后端验收。没有真实外部模型联调、提交或部署。

这些结果只覆盖 2026-11-09 的旧规则，不证明本轮绝对阈值、Token 双上限、单次摘要、90% 安全停机、原子失败报告、Run/SSE 报告引用或前端失败报告同步已经验收。
