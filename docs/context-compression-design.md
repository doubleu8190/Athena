# LLM 上下文压缩设计

状态（2026-11-13）：本方案已完成实施，提交前独立审查 PASS，无有效 Finding；safe-int P2 与安全报告事务 P1 均已关闭，前端 Reviewer 再次 PASS，Root 最终验收通过。P1 修复采用同一完整 `FailRunCommand` 在有效执行权内最多三次幂等重放，专用 typed 异常穿透 execute，Worker 禁止 generic failed 覆盖，并以既有 typed usage 命令尽力保留调用审计。最终证据为后端单元 `907 passed, 1 skipped`、六个 PG 集成文件 `12 passed`，Ruff、`mypy backend/src`、前端 `277 passed`/build/浏览器 e2e `5 passed` 均通过，`backend/src backend/tests` 仅余 9 个既有 typing 基线错误。真实外部 LLM 摘要质量尚未联调；提交状态以 Git 记录为准，本轮未 push 或 deploy，`0024` 未应用到在线数据库。2026-11-09 的历史验收单独列在文末，不作为本轮证据。

## 1. 目标与边界

系统在主模型调用前整理较早会话，使长会话能够在明确的输入预算内继续执行。原始消息和用户可见聊天记录完整保留；摘要只服务当前 Session 的上下文组织，不写入长期记忆，也不能提升为系统指令。

本轮规则包括：

- 常规压缩由管理员配置的绝对 Token 阈值 `T` 触发，不再使用模型窗口乘 80%。
- 近期原文和摘要各有管理员可配置的比例与绝对 Token 上限。
- 不要求压缩后达到 50%、128K 或任何目标水位。
- 每次业务调用前先尝试当前条件下唯一一次合法历史压缩，再用完整业务输入执行固定 90% 安全线和硬窗口检查。
- 达到安全线时停止业务模型与工具，并以无工具报告和现有 `failed` 终态收尾，不新增 Run 状态。
- 当前 Run 的触发、澄清、Execution、工具结果和必要协议交互全部保留。

不包含手动编辑摘要、跨 Session 摘要、长期记忆写入、历史原文删除、全文回查工具或新增业务页面。管理员已有压缩策略界面改为五字段 Token 配置；聊天页需要把失败报告作为目标 Run 的权威消息展示。

## 2. 当前实现

以下描述 2026-11-13 的实际后端代码；测试状态见第 5 节。

| 位置 | 当前实现 |
| --- | --- |
| `backend/src/domain/llm/model/compression_policy.py` | 策略使用 `trigger_tokens`、`recent_ratio`、`recent_token_cap`、`summary_ratio`、`summary_token_cap`；默认建议值为 `262144`、`0.2`、`32768`、`0.1`、`8192`。三个 Token 字段统一限制为 `1..MAX_COMPRESSION_TOKEN_BUDGET`，上限为 JavaScript 安全整数 `2**53-1`。 |
| `backend/src/app/llm/profile_service.py` | 实际策略用例由 `ModelProfileService` 管理，没有独立的 `compression_policy_service.py`。 |
| `backend/src/adapter/llm/model/profile.py`、`backend/src/adapter/llm/repository/profile.py` | 实际持久化模型仍是 `CompressionPolicyVersionModel` 和 `CurrentCompressionPolicyModel`，没有独立的 `compression_policy.py` 模型文件。历史 `trigger_ratio` 列保留以满足旧表结构，新实例只写兼容值、读取新 Token 字段。 |
| `backend/src/app/run/compression.py` | 生产路径按完整主请求测量 `I` 和 `O`；常规触发为 `I >= T`，提前救援只在 `I >= 0.9W` 时发生。旧摘要与全部候选旧历史一次送入一次压缩调用，不拆批、不截断、不切模型、不自动重试；派发后的后置校验异常仍归因为一次已发生调用。压缩失败事件持久化 base summary ID 与候选终点，Worker/审批恢复后从本 Run 事件恢复；同候选或收缩候选直接 skip。 |
| `backend/src/app/run/budget.py` | 压缩回答、报告和业务回答共享 Run 请求计数；最后一次无工具回答槽位规则保持不变。 |
| `backend/src/app/run/executor.py` | 每轮先做上下文准备和安全裁决，再决定是否执行持久化 `resumable batch`。普通、`final_only`、无策略、无压缩槽位和恢复批次路径都不能绕过 90% 或 `I + O > W` 检查。安全报告保留完整当前 Run 材料、使用 canonical 事实行校验，并在报告模型前后分别鉴权。 |
| `backend/src/adapter/run/repository/run.py` | `fail` 可在同一事务中写报告消息、`FAILED`、安全码、usage、报告 ID、失败事件并收敛未完成工具。 |
| `backend/src/api/dto/run.py`、`backend/src/domain/run/model/stream.py` | `RunResponse.report_message_id` 和快照 `payload.report_message_id` 为 `str \| null`；`run.failed` 可选携带 `report_message_id` 和 `context_stop_reason`。 |
| `backend/migrations/versions/20261123_0024_add_context_safety_and_absolute_compression.py` | 线性 head 从 `20261122_0023` 推进到 `20261123_0024`；只增加可为空的新字段和约束，不回填旧浮点策略。 |
| `backend/src/adapter/llm/openai_chat_completions.py` | SDK `AsyncOpenAI` 固定 `max_retries=0`；压缩 `complete` 只调用一次，主回答的既有应用级重试不变。 |

前端 `SessionChatPage.tsx` 已将 `failed` 报告同步与 `cancelled` 早退分开，通过 Run `report_message_id` 或 failed 事件定位目标 Run 在 trigger 之后持久化的 assistant `completed` 报告；文件记录刷新不作为报告同步证据。`useRunStream`、聊天模型和回归用例已由前端独立 Reviewer 验收。

## 3. 已确认产品规则

### 3.1 完整输入与硬窗口

设主模型上下文窗口为 `W`：

- `I` 是主业务请求的完整输入 Token 数，包括 system prompt、工具定义、协议开销、摘要、摘要未覆盖历史、当前 Run 的必要交互、文件、Skill、重试/恢复上下文及其封装。
- `O` 是本次调用单独预留的输出 Token 数。
- 任何业务或报告调用都必须满足 `I + O <= W`；不得发送确定超硬窗口的请求。

业务安全裁决必须使用原业务可用 tools 的完整输入。不能先清空 tools、删除当前 Run 协议或回装未覆盖原文来降低占用，再绕过 90% 检查。最终报告请求另按 `I_report + O_report <= W` 计算，不能把业务 `I` 与报告 `O` 混用。

### 3.2 常规触发

- 默认 `T = 262144` Token，即十进制 `256K`；`K = 1024` 是实施常数建议。
- 管理员可配置 `T`，并随不可变策略版本固定到 Run。
- 常规触发条件是 `I >= T`，不乘 80%。
- 管理员把 `T` 配置得高于 `0.9W`，或主模型窗口很小时，仍不能绕过固定 90% 安全线。
- `trigger_tokens`、`recent_token_cap`、`summary_token_cap` 使用同一 `1..2**53-1` 边界；该限制只保证浏览器整数精确定义和数据库写边界，不允许用当前模型窗口上限压缩合法的绝对触发阈值。

### 3.3 近期原文与摘要预算

- `recent_budget = min(T * recent_ratio, recent_token_cap, model_feasible_budget)`。
- `summary_budget = min(T * summary_ratio, summary_token_cap, model_feasible_budget)`。
- 默认 `recent_ratio = 0.2`、`recent_token_cap = 32768`；默认 `summary_ratio = 0.1`、`summary_token_cap = 8192`。
- `recent_ratio` 和 `summary_ratio` 继续由管理员配置，并沿用两个比例均为 `(0, 1)` 且两者之和小于 `1` 的约束。
- `model_feasible_budget` 必须计入摘要包装、指令、工具、协议和输出预留。压缩模型窗口使用压缩模型 Tokenizer；最终摘要上限再使用实际主模型 Tokenizer复核。

### 3.4 当前 Run 与可压缩范围

- 当前 Run 的触发、澄清问答、全部 Execution 消息、工具调用和工具结果完整保留。
- 只压缩当前触发消息之前、且尚未被兼容摘要覆盖的连续历史轮次。
- 工具调用与结果不能拆开；无法安全识别完整轮次时整段保留。
- 已有摘要覆盖的原文不重复注入，同一覆盖范围不重复压缩。
- 没有新的可压缩历史时直接 `skip`，不写压缩开始/失败事件，不重整摘要。
- 压缩失败候选的稳定身份由 base summary ID 和候选覆盖终点共同记录。相同终点或收缩为已失败范围子集的候选直接复用兼容快照；当前 Run 新增 Execution、工具结果、Skill 提示等不构成候选扩展，不能因此隐式重试。
- 只有候选覆盖终点连续前移，代表旧历史范围真实扩展时，才允许下一次 `prepare` 发起一次新的压缩尝试。不同父摘要/版本仍按实际连续覆盖终点判断，不能被单纯身份字符串变化诱导重复失败范围。
- 失败候选 skip 不调用压缩 Provider、不预占槽位、不写新的开始/失败/完成事件，并保留上次真实失败类别作为后续 90% 收尾原因提示。

### 3.5 摘要复用与窗口变化

- 主模型窗口或管理员阈值变大时继续复用已有兼容摘要，不自动恢复摘要已覆盖原文。
- 摘要不可变，覆盖边界连续且严格早于当前触发消息；父摘要必须属于同一 Session 和 owner。
- 已有摘要超过新的 `summary_token_cap` 时按以下已确认规则处理：
  1. 旧摘要继续完整复用，超过新上限本身不触发重整、拒绝使用或原文回装。
  2. 只有新生成的摘要版本执行新的 `summary_token_cap`。
  3. 没有新的可压缩历史时不重整旧摘要。
  4. 有新的可压缩历史时，旧摘要与新历史合并做一次增量生成，产生满足新上限的新版本。
  5. 旧摘要无论是否超过新上限，都完整计入业务 `I`，并继续参与 90% 与硬窗口检查。

### 3.6 单次压缩与回退

- 一次请求准备中的压缩最多发起一次摘要生成供应商调用。
- 已有摘要、全部尚未覆盖的候选旧历史、压缩指令和输出预留必须一次装入压缩模型；装不下则本次压缩失败。
- 不拆批、不硬截历史、不切换压缩模型、不以中间摘要串联下一次调用。
- 摘要路径不自动重试；Provider/SDK 也不得隐藏重试。超时、网络错误、非正常完成、非法响应和超限直接失败。
- 摘要响应必须正常完成、非空、仅含文本且无工具调用。截断、拒绝、空文本、协议异常或超限不能用字符串硬截断修复。
- 摘要生成失败时保留旧摘要和全部原文，不产生中间摘要或部分版本。
- 只有单次生成成功、压缩模型和主模型实际 Tokenizer 校验通过、有效性校验和最终主请求硬窗口校验全部通过，才原子持久化最终版本。
- 普通压缩失败低于 90% 且兼容快照满足硬窗口时可以继续业务调用；executor 外层禁止重新回装全历史。
- Run 最新 failed 重试、数据库幂等重放和主回答应用级重试属于既有独立机制；它们不是摘要重试。报告调用也不是摘要重试。
- 首次 Provider 调用前发生的输入装不下、模型不可用和预算失败同样写入带候选身份的失败事件，因此 Worker 重建后仍能识别已经失败的范围；历史事件没有候选字段时视为未知，不因此设计兼容迁移。

### 3.7 90% 安全线与继续顺序

固定安全线为 `I >= 0.9W`，管理员不可配置。每次业务模型调用前执行：

1. 读取已持久历史、当前 Run 消息、完整 tools、文件和 Skill 上下文。
2. 存在新可压缩历史且仍有执行权、压缩槽位时，常规在 `I >= T` 触发；`0.9W <= I < T` 时允许一次提前救援压缩。
3. 按压缩后的实际候选重新计算完整业务 `I` 和 `O`。
4. `I >= 0.9W` 时停止业务模型、工具调用和 `resumable batch`。
5. `I < 0.9W` 但 `I + O > W` 时仍因硬窗口不足安全停止；不能发送超窗请求。
6. `I < 0.9W` 且 `I + O <= W` 才继续业务模型和工具执行。

提前救援不改变 `T` 的常规触发语义。当前实现会在普通、`final_only`、无策略、无压缩槽位和恢复批次前执行同一安全裁决。不需要等所有候选摘要批次完成，因为本方案已取消摘要分批；只对业务继续和最终收尾做硬校验。

失败候选 skip 只影响是否再次调用压缩 Provider，不短路 90% 与硬窗裁决。若同时发生压缩失败和 `I + O > W`，对外 `context_stop_reason` 仍按既有优先级为 `hard_window_exceeded`，但内部报告状态和 canonical“停止原因”必须同时保留真实压缩结论，例如 `compression_input_too_large` 或 `compression_failed`。报告模型不能删除这条事实行。

### 3.8 安全报告与原子失败终态

安全码固定为 `run_context_safety_stop`。负责原因字段固定为 `context_stop_reason`。

安全停机后的报告要求：

- 使用无 tools 的业务模型生成报告，并明确包含“已完成”“产出”“未完成”“停止原因”四部分。
- 不得宣称任务完成；Run 仍进入 `FAILED`。
- 原因需区分无新历史、压缩未配置、压缩模型不可用、压缩预算耗尽、压缩输入无法一次装入、压缩调用失败、压缩后仍高和业务硬窗口不足。
- 平台先基于持久状态生成固定四节和固定事实行的 canonical 系统报告。报告模型只能在各节内重排事实行；模型输出必须逐字保留全部权威事实行各一次，匹配章节归属，不能新增、删除、改写、重复或改变状态。事实行集合或多重集不一致、出现未知 file ID、声明未发生的完成或遗漏持久记录时，直接使用系统报告。
- 系统兜底只展示安全元数据，不粘贴完整超大正文；但必须让用户看见当前 Run 的文件引用、输入文件、已加载 Skill 标识与版本、重试来源、工具状态、安全可见 artifact 和准确停止原因。
- 系统报告的固定事实行是模型输入的一部分。报告模型状态可以省略与目标 Run 无关的旧历史，但必须包含当前 Run 全部文本与工具调用/结果、全部合法文件引用和输入文件、完整已加载 Skill 上下文、安全可见 artifact、工具状态及 retry 上下文；不得截断、摘要或硬裁剪。完整必要材料无法安全入窗时零报告模型调用并直接系统兜底。
- 报告请求使用独立 `I_report`，只要求 `I_report + O_report <= W`；允许 `I_report >= 0.9W`。
- 报告调用共享 Run 总请求上限，不新增调用总量，不重置 execution deadline。
- 报告模型调用前必须完成租约、取消、deadline 和鉴权检查；模型返回后、写报告及失败终态前必须再次完成租约、取消和鉴权检查。后置鉴权失败时按 `run_auth_session_invalid` 等既有普通错误收敛，不能写安全报告、报告 ID 或安全 failed payload；已经发生的报告调用仍保留其槽位和已知/未知 usage。

报告消息与失败状态的原子规则：

- `fail` 在同一事务中写报告消息、`FAILED`、`safe_error_code`、已知 usage、`report_message_id`、失败事件，并收敛未完成工具。
- 不允许先 `finish` 为 `completed` 再转 `failed`。
- 不允许失败事件已提交而报告稍后补写。
- 报告必须是目标 Run/Session/owner 的 `assistant`、`completed` 消息，`reply_to_message_id` 和 `context_turn_id` 均为触发消息，sequence 严格晚于触发消息。
- 硬窗覆盖外部停止原因时，报告事实仍同时包含主请求硬窗和最近一次真实压缩失败类别；模型只可重排 canonical 事实行，遗漏任一事实都会回退系统报告。
- 同一稳定报告 ID 加安全码重放返回既有终态，不重复写消息、usage 或事件。
- 取消、失租、execution deadline 和鉴权失效优先于报告；失效执行不能越权补写。
- 安全失败持久化无法提交时，executor 不把它当作正常结束，交给 Worker 现有收敛路径处理。

不新增 Run 状态。客户端通过 Run 的 `report_message_id` 或 `run.failed.report_message_id` 精确定位报告。

### 3.9 事件、usage 与 SSE

- 压缩开始、完成、失败和 Run failed 事件不写入摘要或报告正文。
- usage 继续按 `purpose=answer|compression`、调用、重试和执行代次独立记录；已知值累计，供应商未提供值保持未知，不填零。
- 摘要预检装不下时 `batch_count=0`，供应商调用为零、Run 请求槽位消耗为零，不写伪 `usage=0`。
- `compression_failed` 事件新增可选的 `candidate_base_summary_id`、`candidate_covered_through_sequence` typed JSON 字段；它们是事件载荷字段，不要求新数据库列或迁移。旧事件缺少字段时按未知处理。
- 摘要调用派发后固定 `batch_index=0`、`attempt=1`、`batch_count=1`，消耗一个 Run 请求槽位并保存已知 usage；未知字段保持 `null`。
- 摘要调用前显式设置派发状态。Provider 返回后若输出 Tokenizer、最终主上下文测量等后置校验抛出普通 `RunContextError`，失败事件仍记为 `batch_count=1` 并保留本次调用槽位和已知 usage；取消、失租和 execution deadline 继续终止，不伪装成可回退的普通压缩失败。
- 旧事件/usage 的 `batch_index`、`batch_count` 结构保留，只表示单次调用，不引入历史字段迁移。
- SSE 发布继续是持久化后的尽力推送。断线重连从 PostgreSQL 快照、Run `report_message_id` 和 failed 事件恢复，不依赖瞬时文本。

### 3.10 既有不变量

本轮取消摘要分批和自动重试，不取消以下既有要求：

- 增量摘要顺序连续、覆盖不跳段、父摘要同 Session/owner，覆盖终点严格早于触发。
- 单次摘要请求必须先通过完整输入预检和独立输出预留检查，再调用 Provider；业务最终请求还会重新检查。
- 摘要正文必须是完整、有效、非截断、无 tools 的文本；不得硬截字符串。
- 最终摘要使用实际主模型 Tokenizer复核，压缩请求使用压缩模型 Tokenizer。
- 生成过程中不落中间或部分摘要，全部成功后才写最终版本与完成事件。
- 摘要提交和完成事件同事务，按 Session → Run 锁顺序复核 Session、owner、Run `running`、取消、worker、epoch、租约和 deadline。
- 操作 ID 和数据库幂等重放保留；仅最新 failed Run 可按 `created_at DESC, id DESC` 重试，并复用源触发与 cutoff。
- 业务工具调用批次和 `resumable batch` 保留，继续保存、恢复、收敛未执行调用，并在执行副作用前做上下文安全裁决。
- 取消、失租、鉴权和 deadline 检查保持既有优先级。

## 4. 技术实现与前端契约

### 4.1 后端技术选择

- `K` 使用 `1024`，默认值为 `262144/32768/8192`。
- 管理员接口字段固定为 `trigger_tokens`、`recent_ratio`、`recent_token_cap`、`summary_ratio`、`summary_token_cap`；仅删除 `trigger_ratio`。三个 Token 字段在 DTO、service 和 repo 写边界均执行 `1..MAX_COMPRESSION_TOKEN_BUDGET` 校验，越界 API 输入返回 422，不能进入数据库形成 500。
- 未配置当前策略时保持现有“压缩未启用”机制，不自动创建默认策略；90% 保护仍覆盖该路径。
- 历史 `trigger_ratio` 列保留以避免改写旧版本表；新格式字段不完整时投影为不可用策略，不把旧 float 静默解释为 Token。
- 既有比例型策略的兼容、回填和自动转换不在本轮范围；迁移只保证现有 DB 可升级，Run 与外键引用不被改写。
- Run 和快照通过 `report_message_id` 暴露报告；failed 事件通过可选的 `report_message_id`、`context_stop_reason` 暴露同一信息。

### 4.2 前端必须遵守的合同

- 管理弹窗保留五个可编辑字段，只移除 `trigger_ratio`；派生值基于当前实际比例展示。
- `SessionChatPage` 必须将 `failed` 与 `cancelled` 分开。`failed` 先读取 `run.report_message_id` 或 failed 事件，再读取目标 Run 的权威报告消息；`cancelled` 保持无报告。
- 报告必须晚于触发消息，且匹配目标 `run_id`、`assistant`、`completed` 和 `reply_to_message_id`。不能用“最新 assistant 消息”代替。
- `useRunStream` 和快照解析 `report_message_id`；SSE 重连、刷新和事件分页都必须恢复到同一报告。
- 刷新文件记录不能作为报告已同步的证据。

## 5. 验收与证据

已确认行为验收：

- `T`、`0.9W` 和 `I + O <= W` 的取整与边界稳定；`T > 0.9W` 不能绕过 90%。
- 当前 Run、文件、Skill、重试、恢复协议和完整 tools 均计入业务 `I`。
- `final_only`、无策略、无压缩槽位和恢复批次前都执行 90% 与硬窗口裁决。
- 无新历史时不写压缩事件、不重整摘要。
- 同终点失败候选在后续 `prepare`、Worker/审批恢复或重新创建 `ContextCompressor` 后直接 skip；仅增加当前 Run Execution、工具结果或 Skill 提示时不调用 Provider、不预占槽位、不新增压缩事件，并保留上次失败原因。即使业务预算已耗尽，已记录的同候选失败原因也优先于 `compression_budget_exhausted`。
- 失败候选的旧历史终点真实前移时允许一次新尝试；候选收缩或终点不变时不重试。只有真正扩展后的新候选遇到无压缩槽位时，才提示压缩预算耗尽。
- 旧摘要超过新 cap 时继续复用；无新历史不回装原文；有新历史才产生满足新 cap 的增量版本；旧摘要导致的 90% 仍停止。
- 候选装不下时零 Provider 调用、零槽位、无伪 usage；调用已派发时只一次，失败不重试，已知/未知 usage 正确记录；Provider 成功后的 Tokenizer 或最终测量异常仍保持一次调用、一个槽位和已知 usage。
- 压缩失败不出现中间摘要或部分落库，兼容快照安全时继续，否则报告。
- 安全报告无 tools、允许 `I_report >= 0.9W`，但必须满足报告硬窗；模型只能重排 canonical 事实行，新增、缺失、改写、重复、错放或未知 file ID 均回退系统报告。
- 压缩输入过大或压缩调用失败与主业务硬窗同时发生时，报告同时包含硬窗和真实压缩结论；对外失败事件的 `context_stop_reason` 优先级保持 `hard_window_exceeded`。
- 报告后置鉴权被撤销时走普通鉴权失败，不写报告或安全 failed payload。
- 报告与 `FAILED` 原子幂等，sequence 晚于触发；取消竞争优先；不出现 `completed -> failed`。
- 安全收尾的完整 `FailRunCommand` 只构造一次；发生不确定写错误时可基于稳定报告 ID 和已有幂等语义，在 deadline、lease、auth 仍有效时最多提交三次，期间不重新调用报告模型、不重复增加预算槽位。
- 三次提交均无法确认持久化时抛出专用 `RunContextSafetyCommitError`；`RunExecutor.execute` 原样传播，Worker 不会改写为无报告 `execution_failed`，仍为 `RUNNING` 的行交由既有租约回收。取消、失租、deadline、鉴权或领域状态拒绝优先，不继续重放。
- 上述终态不可写时，已发生的报告调用通过现有 typed `RecordRunUsageCommand` 尽力保存；未知字段保持未知，不填零。数据库整体不可写时生命周期优先，保留 typed 异常而不伪造普通失败。
- Run API、快照和 failed 事件均能提供报告 ID；SSE 重连恢复同一报告。
- 压缩策略三个 Token 字段在 DTO、service 和 repo 统一接受 `1..2**53-1`；上限加一和 `2**80` 在 API 返回 422，service 直连输入被拒绝，repo 不写入越界策略行。

本轮实际证据：

- 第 2 轮候选去重定向回归：`backend/tests/test_run_compression.py` 为 `25 passed`；覆盖 Provider 失败后仅增加当前 Run Execution、Worker/Compressor 重建恢复、0-call 预检失败、模型不可用、候选真实扩展、最后预算槽位下保留既有失败原因。`backend/tests/test_run_executor_tools.py` 的相关硬窗/无策略闸门定点用例为 `3 passed, 38 deselected`。
- 提交前 safe-int P2 定向回归：`backend/tests/test_llm_profile_service.py` 为 `24 passed`，覆盖三字段 MAX 接受、MAX+1/`2**80` DTO 与 service 拒绝以及 HTTP 422；`backend/tests/integration/test_llm_profile_postgres.py` 为 `1 passed, 1 warning`，验证 MAX 可持久化、越界值不新增策略行。
- 提交前报告提交 P1 定向回归：`backend/tests/test_run_executor_tools.py backend/tests/test_run_worker.py` 为 `56 passed`；覆盖 execute 级首事务失败后同一命令重放、持续失败 typed 穿透、Worker 不写 generic failed、失租/仓储拒写停止重放、已知 usage 审计以及鉴权和取消优先路径。
- 报告原子事务定向 PG 回归：`test_context_report_and_failed_state_are_atomic_idempotent_and_cancel_aware` 与 `test_failed_report_insert_rolls_back_run_usage_and_events` 为 `2 passed, 2 warnings`。安全 E2E owner 新增 normal 与 report-event-write-retry 参数后真实 PG 两例通过；当前六个集成文件合计为 `12 passed`，故障发生在报告消息 flush 后 failed event INSERT，完整命令重放字段一致，最终仅一份 message/event、一个 LLM slot 和一次 usage attempt。
- Root 前轮非集成后端回归：`893 passed, 1 skipped, 52 deselected, 5 warnings`，耗时 `39.44s`；这是 safe-int/P1 修正前的主体记录。P1 修复前中间单元记录为 `903 passed, 1 skipped`；当前最终结果 `907 passed, 1 skipped, 53 deselected, 5 warnings`，耗时 `37.33s`，覆盖并更新中间数字。
- Root 前轮 PostgreSQL/Redis/SSE 六个集成文件回归：`11 passed, 14 warnings`，耗时 `19.92s`。P1 修复后的当前最终回归为 `12 passed, 15 warnings`，耗时 `30.93s`；包含真实 HTTP→Worker→PG→Redis→SSE 安全停机及 report-event-write-retry 用例，覆盖安全 FAILED/report、系统报告兜底、完整 current Run trigger、独立报告硬窗、无业务调用、丢失通知和重连同报告。
- Root Ruff `backend/src backend/tests`：通过。
- Root 新增格式检查 5 个文件通过：`backend/src/adapter/llm/repository/profile.py`、`backend/src/app/run/compression.py`、`backend/tests/test_run_stream.py`、`backend/src/domain/run/model/entities.py`、`backend/tests/integration/test_context_safety_e2e_postgres.py`。
- Root mypy `backend/src`：305 个文件无错误；`backend/src backend/tests` 仍为 9 个既有无关基线错误，没有新增错误。
- 前端未受本轮影响，Root 复核仍为 `277 passed`、build 通过、浏览器 admin e2e `5 passed`；前端独立 Reviewer 为 PASS。

上述主体实现证据已由前后端独立 Reviewer 审查。提交前 safe-int P2 已由 fresh Reviewer 确认通过；随后发现的安全报告提交 P1 经定向修复后，fresh Reviewer 复审 PASS，无有效 Finding，定向测试 `5 passed`，前端再次 PASS。Root 已对冻结后的最终代码完成验收，P2 与 P1 均关闭。历史 2026-11-09 的通过数字不用于证明本次功能。

## 6. 主体验收、提交前修正与实施记录

产品规则与实施范围已冻结。主体实现、两轮主体返修、提交前 P2/P1 修复、独立审查和 Root 最终验收均已完成；提交以 Git 记录为准。

2026-11-13 的返修与验收记录：

- 第一轮后端 Reviewer 的 4 项 Finding 已关闭：安全报告写前再次鉴权；完整当前 Run 报告状态与 canonical 事实保护；Provider 已派发后的 Tokenizer/最终测量异常保持 1 次调用、1 个槽位和已知 usage；真实 HTTP→Worker→PG→Redis→SSE 安全停机回归。
- 第二轮失败候选去重 P1 已关闭：`compression_failed` 持久化 base summary ID 与候选终点，同候选或收缩候选在后续 `prepare`、Worker/审批恢复和 Compressor 重建后均 skip；只有连续覆盖真实前移才允许新尝试；最后业务槽位及硬窗收尾继续保留真实压缩失败原因。
- 后端 Reviewer 第 2 轮 PASS，无有效 Finding；前端 Reviewer PASS。
- 前轮 Root 实测后端 `893 passed, 1 skipped, 52 deselected`、PostgreSQL/Redis/SSE 六个集成文件 `11 passed`，前端 `277 passed`、build 通过、浏览器 admin e2e `5 passed`；Ruff、指定 5 文件格式检查和 `mypy backend/src` 通过。safe-int 后、P1 修复前的中间单元记录为 `903 passed, 1 skipped`。
- 提交前复核发现压缩策略三个 Token 字段缺少统一 JavaScript 安全整数上界。现已在 DTO、service 和 repo 写边界统一为 `1..2**53-1`，并新增 MAX 接受、MAX+1/`2**80` 拒绝、HTTP 422 及 PostgreSQL 不落越界行回归。
- fresh Reviewer 确认 safe-int P2 通过后，又发现安全报告事务异常会被 `execute()` 和 Worker 连续降级为无报告普通失败。现已改为一次性完整命令、最多三次同命令幂等提交；专用 `RunContextSafetyCommitError` 穿透 execute，Worker 对 `RUNNING` 行禁止 generic 覆盖，取消/失租/deadline/鉴权拒绝优先，已知 usage 通过既有 typed 审计命令尽力保存。
- fresh Reviewer 对 P1 修复复审 PASS，无有效 Finding，定向测试 `5 passed`；safe-int P2 与安全报告事务 P1 均关闭，前端 Reviewer 再次 PASS。
- Root 对冻结后的最终代码验收通过：非集成后端 `907 passed, 1 skipped, 53 deselected, 5 warnings`，耗时 `37.33s`；PostgreSQL/Redis/SSE 六个集成文件 `12 passed, 15 warnings`，耗时 `30.93s`；Ruff、`mypy backend/src` 305 files 与 `git diff --check` 通过。前端未修改，仍为 `277 passed`、build 通过及浏览器 admin e2e `5 passed`。

剩余限制明确保留但不再阻塞本轮：

- 未运行真实外部 LLM 质量联调，确定性 Provider 不能替代供应商兼容性和摘要质量证据。
- `mypy backend/src backend/tests` 仍有 9 个既有无关基线错误；不在本轮扩修。
- 修改范围外的旧 format 差异未整体重排；仅保证本轮新增格式的 5 个文件通过。
- 既有比例型策略的兼容、回填或自动转换不在本轮范围。
- 提交状态以 Git 记录为准；本轮未 push 或 deploy，`0024` 未应用到在线数据库。

## 7. 历史实施记录（2026-11-09）

2026-11-09 的旧方案曾实现 80%/20%/10% 比例触发、分批摘要、每批重试、策略版本、Run 事件和失败回退。当时后端历史全量结果为 `516 passed, 3 skipped`，另有静态检查和 PostgreSQL/Redis 竞争测试；独立 Reviewer 第 2 轮为 PASS，Root 完成后端验收，但没有真实外部模型联调、提交或部署。

这些分批和重试只属于历史实现。其测试不能作为 2026-11-13 绝对阈值、单次摘要、90% 安全停机、原子失败报告或前端失败报告同步的通过证据。
