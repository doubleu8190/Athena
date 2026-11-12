# LLM 上下文压缩实施方案

状态：实施完成，独立 Reviewer 第 2 轮结论为 PASS，Root 于 2026-11-09 完成后端验收。产品规则以 `docs/context-compression-design.md` 第 3 节为准；真实外部模型联调尚未执行。

> **模块状态（2026-11-12）**：旧 `LlmProvider`、`LlmService` 和 Responses 低层模块已删除；压缩与主回答均通过固定 runtime 配置的 Chat Completions Adapter 执行。下文涉及旧 Provider/Responses 的表述仅为历史实施记录。

## 1. 实施基线与交付目标

当前工作树已具备 Run、管理员模型版本、Chat Completions Adapter、Worker、数据库事件和 SSE。本方案在现有结构中增加压缩闭环，不增加新 Worker、任务队列、通用 Trace 表或独立摘要服务进程。

主调用前按完整预算达到 80% 触发；近期历史预算 20%、摘要上限 10%，均可配置。当前 Run 完整保留，原消息不改写；增量摘要可分批，最终校验成功后原子保存。失败可回退，取消、超时、失租优先终止执行。

仅最新且 failed 的 Run 可重试，重试沿用源触发消息和 cutoff；成功请求幂等重放仍返回原结果。不得重新引入任意历史 Run 回退。

交付范围为后端配置、上下文生成、持久化、状态流和测试文档。真实模型联调单独记录环境与结果；不包含前端页面、长期记忆、原文检索工具、摘要人工编辑或部署。

## 2. 当前代码核对

| 位置 | 事实与实施影响 |
| --- | --- |
| `app/run/context.py` | 现有计数、消息转换和有界 tokenizer 线程可复用；需拆开“估算”和“硬窗口拒绝”，否则无法在超窗时先压缩 |
| `app/run/executor.py` | 目前直接读取上下文并调用主模型；在该准备阶段插入压缩，不改变现有主回答/澄清提交语义 |
| `adapter/run/repository/run.py` | 已有 Session → Run 锁、执行权校验、retry 最新校验及原 cutoff；新增命令必须沿用这些规则 |
| `adapter/run/model/run.py` | 单 Session 活跃 Run 唯一索引已存在；无需另建 Session 压缩锁 |
| `domain/run/model/entities.py`、`adapter/run/converter/run.py` | usage 与事件是 typed JSON，需扩展类型、事件映射及旧数据读取兼容 |
| `api/dto/run.py`、`api/converter/run.py` | 对外 usage 有独立 DTO，不能只改领域模型；摘要正文与凭据不得透出 |
| `adapter/llm/openai_chat_completions.py` | 仅 runtime stream，当前能力校验同时要求 stream/tools；complete 必须支持无 stream/tools 模型，不能复用该校验原样调用 |
| `domain/llm/ports.py` | 旧 `LlmProvider` 已删除；当前 `LlmRuntimeTextProvider` 使用固定 runtime 配置，不把持久模型 ID 当旧 Profile 名称 |
| `adapter/llm/repository/profile.py` | 现有模型版本和事务内管理员复核可复用；压缩默认指向独立于主模型默认指向 |
| `app/run/stream.py`、`domain/run/model/stream.py` | 已有持久事件重放、数据库对账和快照；压缩只增加 typed payload，沿用传输 envelope |
| `run_worker.py`、`main.py` | 分别组装 Worker 与 API 依赖；压缩共用 Worker 内的 LLM Adapter 实例和配额 |
| `backend/migrations/versions` | 文件检查目前链尾为 `20261111_0012`；执行时重新读取 Alembic head，不预先占用下一编号 |

路径省略的代码均相对 `backend/src`。当前分支 `v2` 有大量未提交的相邻功能，开始实施前记录状态与阶段差异，禁止重置或混入他人修改。原 Athena 仓库保持只读。

## 3. 具体技术落点（随本方案确认）

### 3.1 配置归属与管理接口

压缩策略归 `llm` 域：在 `domain/llm` 定义策略输入、不可变版本、仓储端口；在 `app/llm/compression_policy_service.py` 编排；在 `adapter/llm` 下按 model/repository/converter 实现。摘要及其执行命令归 `run` 域，避免两个域重复管理同一配置。

建议新增两个配置表：`llm_compression_policy_versions` 与单行 `llm_current_compression_policy`。策略版本保存 compression_model_version_id、trigger_ratio、recent_ratio、summary_ratio、prompt_template_version、created_at；当前指向可为空，表示尚未启用。比例按有限数值校验，均在 (0,1)，recent_ratio + summary_ratio < 1；默认 0.8/0.2/0.1。

管理员接口建议为 `GET /api/v1/admin/llm/compression-policy` 读取当前策略，`PUT` 同路径校验后创建不可变版本并切换指向。未配置时返回显式未配置状态；不新增普通用户配置入口。沿用 Origin、管理员鉴权和事务内登录态/角色复核。模型只需支持现有 Chat Completions 文本生成，不要求 supports_stream 或 supports_tools；策略设置时校验版本存在且启用，凭据有效性沿用现有机制。

`runs` 新增可空 compression_policy_version_id 外键。submit/retry 在创建事务中绑定当前策略；answer 恢复不更新该字段。策略不存在或旧 Run 为 null 时保持旧逻辑，不回填、不影响 Run 创建。已绑定策略不可原地修改；实际每批调用前复核压缩模型启用状态及凭据，不自动换模型。

配置并发按现有管理员事务规则处理；Run 读取配置的锁放在 Session → Run 之后，不引入反向锁。切换时以事务提交顺序决定新 Run 使用哪个完整版本，不读取半套参数。

### 3.2 摘要持久化

在 `adapter/run/model/context_summary.py` 新增 `session_context_summaries`，领域实体与命令放 `domain/run/model/compression.py`，converter 放 `adapter/run/converter/context_summary.py`。字段遵循设计第 4.2 节，不保存中间批次。

外键需保证 Session/owner、source Run/Session、parent summary/Session 归属一致；主键加必要复合唯一键支撑组合外键。covered_through_sequence 为正，且提交校验其为实际 Conversation 前缀边界、严格早于触发消息。parent 边界不得晚于新边界；仅重整摘要时允许相同边界，不得逆退或跳过原文。

索引覆盖 `(session_id, owner_id, covered_through_sequence, created_at, id)`；压缩操作标识按 Run 范围唯一，构成提交幂等键。读取按覆盖最远、创建时间/id 排序，分页检查候选，不能固定取一条后就错误宣称没有兼容版本。无需 Session 最新摘要指针。

扩展 RunRepository 提供边界安全的摘要选择、原文区间读取、压缩开始/失败事件与最终摘要提交命令。应用层不接收 AsyncSession。摘要提交事务验证 Session 未删除、owner、running、无取消、worker/epoch、租约和 deadline；插入摘要、追加 completed 事件同事务，发布在提交后。重复提交返回已保存结果，不重复产生事件。

### 3.3 调用记录与用量

扩展现有 RunAttemptUsage 和 RecordRunUsageCommand，而不新建通用调用表。调用键为 epoch、purpose、operation_id、batch_index、attempt；主回答沿用其现有调用序号，压缩批次各自计重试。记录模型版本 ID、耗时及已知 Token 数，按 purpose 汇总，并保留包含全部调用的总量。

旧 JSON 缺失 purpose 时解释为 answer，新增可选字段给出明确默认值；旧 attempts 和原汇总可正常读取，不回写未知成本。不能让历史聚合值因缺少逐次记录而被归零。未知 usage 保持 null，complete 反映完整性。同步 API DTO/converter；管理员通过结构化分类用量排查，不增加原始摘要或供应商错误体输出。

压缩操作 ID 在一次请求准备内稳定，开始/失败事件、批次用量及最终摘要共用此 ID。epoch 变化生成新操作，记录不会覆盖其他执行代次。失租后不补写；租约终止前已知失败调用的 usage 也要记录。

## 4. 分阶段执行

所有阶段由同一 Implementer 顺序实施，测试随阶段编写和执行。阶段交付记录自身修改清单；累计完成后交 Reviewer 审查实际新增文件及 tracked/untracked Diff。

### S1：领域契约、模型与迁移

1. 新增压缩策略、摘要实体、操作结果、typed 事件和仓储命令，扩展 Run 策略引用与 usage。
2. 新增上述三张表与可空 Run 引用；登记 `adapter/models.py` 和各模块导出，确保 Alembic 可发现。
3. 更新 converter 的旧 usage 兼容；所有新数据库 JSON 明确类型化，复用 `common/json.py`；时间使用 `common/date.py`，ID 使用现有工具。
4. 新迁移升级不改消息、不自动生成摘要，不重写 `0011/0012`。数据降级会丢失压缩配置和摘要，必须在文档明确备份要求及不兼容写入停止步骤，不能承诺业务无损降级。

验收：隔离 PostgreSQL 空库升级及从当前 head 带历史 Run/usage/消息升级均成功；旧 JSON 可读，原数据不变；组合归属约束拒绝跨 Session/owner 引用。迁移测试使用独立 schema，不操作生产数据。

### S2：管理员配置与摘要仓储

1. 完成策略 service/repository、API DTO/converter/router，在 `main.py` 注册。
2. 在 submit/retry 绑定策略快照，保持请求幂等重放先于新配置读取，恢复沿用原快照。
3. 实现兼容摘要查询、原文读取及带执行权校验的事件/摘要写入。借用现有锁顺序和数据库时钟，不持锁调用 LLM。
4. 为摘要正文设置与会话数据相同的可见性；软删除 Session 后不可读，不增加摘要正文 HTTP 接口。

验收：普通用户配置被拒绝；管理员身份撤销后写入失败；策略切换与 Run 创建只绑定完整版本；重复请求不重复建 Run/摘要；最新 Run 重试和跨 owner 隔离继续成立。

### S3：预算、切分与提示模板

1. 将 `context.py` 拆分为消息转换、完整预算估算及硬窗口校验；估算仍在现有有界线程内，保留超时与槽位释放规则。
2. B = W - O - F，F 包括主提示、工具和固定协议；摘要封装开销计入完整请求。比例预算向下取整，不能得到正摘要额度时按失败处理，不能靠最小值强行越界。
3. 当前 Run 的触发消息及 Execution 全保护。此前正式 Conversation 按时间顺序形成连续轮次：以用户发起任务为边界，原 Run/输入请求关联用于将澄清问答合并回同一轮；失败重试共享触发消息，不形成重复轮次。旧无 managed 关联的数据按 user 开始、后续 assistant 归同轮；不确定能否安全拆分的连续段整体视为一组。Execution 不从其他 Run 额外加载。
4. 从尾部保留能装入 20% B 的完整连续后缀，剩余前缀压缩。下一轮装不下即停止，不能越过它挑更早的小轮次。工具调用与结果成组，非法协议明确失败。
5. 选择未覆盖原文并增量生成；已有摘要只作为标记清楚的历史数据注入，不提升为 system 指令。加入版本化摘要提示和主提示解释，区分摘要中的旧指令与当前用户请求。
6. 单次准备最多一次压缩流程。没有新增前缀且摘要仍兼容时不再压缩；摘要因新窗口超限时允许一次仅摘要重整，不要求消费新轮次。常规分批每批必须消费新轮次，且输入可装入压缩窗口。

验收：准确覆盖阈值边界、B 非正、摘要 10%、原文 20%、完整轮次、澄清恢复去重、共享触发消息重试、跨 tokenizer 计数和无进展终止。除摘要重整外不允许同边界无限重复生成。

### S4：runtime 压缩模型调用

1. 在 `domain/llm/ports.py` 增加 runtime complete 文本能力，Chat Completions Adapter 显式实现；保留原流式接口。历史 Responses 行为已随模块删除，不再是兼容目标。
2. complete 与 stream 共用客户端、按模型版本的 semaphore、超时、响应大小限制和错误映射。complete 不检查 stream/tools 能力，不传业务工具。SDK 隐藏重试继续关闭。
3. 为非流式正常文本响应转换成现有 LlmResponse，显式校验完成原因、拒绝、空文本、工具调用及长度截断。不因 HTTP 200 就接受摘要；压缩内部结果不发用户文本 delta。
4. 逐批计算压缩模型 tokenizer 输入与输出预算；摘要输出请求上限受该模型 max_output_tokens/窗口约束。响应再按主模型 tokenizer 校验 10% B，不能把两种 tokenizer 的 Token 数视为等价。
5. 临时网络/限流/服务端故障每批最多三次（含首次），指数退避且尊重剩余 deadline。仅未获得模型输出的失败可重试；完整但不合格的摘要、已收到输出后中断均不自动重试。若 complete 无法证明异常前无输出，保守不重试。认证、协议、超窗不重试。

验收：mock SDK/传输验证无工具、无流式能力配置可用；主回答仍要求自身能力；同模型压缩与回答共享配额；等待配额、请求、退避均可取消且计入总时限；错误、取消与成功均释放资源。

### S5：Run 编排、事件与回退

1. 新增 `app/run/compression.py`，由 executor 在主模型调用前委派准备上下文。`run_worker.py` 注入策略/摘要能力及同一个 Adapter，不另建主调用配额实例。
2. 先构造本次回退上下文：兼容旧摘要＋未覆盖原文；没有兼容摘要则读取 cutoff 内原始历史。摘要不兼容不能直接抛弃其覆盖原文。无策略的 Run 完全沿用旧逻辑。
3. 达阈值且有必要压缩时写 started 事件，执行增量/分批；中间摘要仅驻内存。全部成功后重算主请求，再原子提交摘要与 completed 事件。
4. 普通失败写 compression_failed 安全事件，回退请求能装入硬窗口且执行权/deadline 有效则继续，否则以明确错误结束 Run。不拼接中间结果，不静默截断。没有历史可压缩且受保护输入超窗直接失败。
5. 取消、超时、失租不进入普通回退，关闭供应商资源，依现有 Run 终态规则收敛。摘要已提交后主模型失败不删除该版本，最新失败 Run 可重试复用它。
6. 扩展事件 union、converter 映射、API 和 SSE 编码，保持现有 envelope。快照/数据库对账能恢复压缩状态；started 未匹配完成但 Run 已终结时，展示由终态收敛，不永远保持压缩中。
7. 幂等记录每批调用耗时/usage，completed 表示摘要已持久化，不表示主回答成功；failed 表示本次压缩失败，不自动等价于 Run 失败。

验收：正常增量、多批次、批次中途失败、摘要超限、旧摘要不兼容回查、回退继续/超窗失败、完成后主模型失败、多轮澄清、取消和失租等闭环均覆盖。

### S6：并发验证、联调与交付

在真实 PostgreSQL 中用同步屏障构造摘要提交与取消、租约过期、Session 删除和重复提交竞争；证明失效 epoch 不能写摘要/事件/usage。检查新配置锁不破坏原 Session → Run 锁顺序。

在已有 ASGI API＋Worker＋PostgreSQL＋Redis 测试链加入确定性摘要 provider，验证 SSE 状态、发布失败后的数据库对账、重连恢复、消息原文不变和正文不泄露。模拟 provider 结果不能作为真实供应商联调证据。

更新 README：配置压缩模型/策略、默认比例、旧 Run 兼容、用量分类、错误/取消行为及迁移。只有确有新增环境变量时才修改 `.env.example`；默认比例及模型选择持久化到策略，不另加同名环境配置来源。

同步设计中现有模块行为说明；Run 设计/实施文档以新增范围说明链接本方案，保留原阶段验收历史，不把原“首期不压缩”改写成当时已经实现。

## 5. 测试与执行检查

建议新增测试文件（可按现有组织拆分，不要求机械保持名称）：

| 测试组 | 主要证据 |
| --- | --- |
| `test_compression_policy_service.py`、管理员 API 测试 | 参数、权限、Origin、停用模型与策略快照 |
| `test_run_context_compression.py` | 预算、连续轮次切分、去重、摘要选择、跨模型校验、无进展终止 |
| `test_run_compression.py` | 分批、重试、回退、取消、deadline、事件和用量幂等 |
| 现有 `test_llm_chat_completions.py` | runtime complete 转换、完成原因、配额、取消和资源清理 |
| `integration/test_context_compression_postgres.py` | 迁移、组合约束、事务回滚、执行权竞争和最新 Run 重试 |
| 现有 Run stream/end-to-end 测试 | API/Worker/数据库/Redis 联合闭环与状态恢复 |

先运行阶段测试和受影响 Run/LLM 回归，再执行仓库 `.venv` 中 pytest、ruff check、修改文件 ruff format --check 和 mypy（配置范围为 backend/src 与 backend/tests）。实施前记录既有失败，避免将未改文件的格式差异混入本功能。此文档编写阶段不运行业务测试，也不引用历史通过数作为本功能结果。

真实 PostgreSQL 测试沿用 `ATHENA_TEST_DATABASE_URL` 和随机 schema；Redis 联合测试沿用已有连接方式；真实模型沿用 `llm_live` opt-in。记录缺失环境与跳过项，不输出凭据，不因缺少真实模型而停止可独立完成的测试。

联调至少验证同一模型兼作回答与压缩、独立小窗口压缩模型、长会话分批、提前触发失败后回退、硬超窗失败、取消、配置切换后的最新 Run 重试。记录模型 ID/协议、窗口/tokenizer 与观察到的估算差异，检查摘要保留目标/约束/决定/关键结果/待办，不将程序长度验证等同于语义无损。

## 6. 验收与未确认项

用户已整体授权本方案实施，技术落点包括策略归 llm 域、三张新增表、Run 创建绑定策略、当前策略 GET/PUT、runtime 非流式文本调用、typed usage 分类和一次摘要重整例外。

方案已获确认并完成本轮实施。实现前基线与差异快照保存在 `/tmp/athena-context-compression-status-baseline.txt`、`/tmp/athena-context-compression-tracked-baseline.patch` 与 `/tmp/athena-context-compression-untracked-baseline.tar.gz`；实施期间保留了已有 Run/LLM 及其他工作树改动。

按 `AGENTS.md` 执行 Root → luna_implementer → terra_reviewer → Root。Reviewer 必须读取实际 Diff，覆盖新增未跟踪文件，并给出 PASS 或 CHANGES_REQUIRED；每条 Finding 含位置、触发、影响和建议。Root 验证有效问题交原 Implementer 修复，每次修复后重新 Review，最多三轮，超出停止并汇报。

最终验收须同时满足已确认行为、必要测试通过、无未处理阻断 Finding。报告文件范围、实际检查结果及环境限制；不以文档自查、测试通过或阶段实现替代独立 Review，不自动提交、推送或部署。

## 7. 实施与修复记录（2026-11-09）

- S1–S5 主体已实现：策略版本和 Run 快照、摘要版本、完整请求预算、runtime 非流式文本调用、增量/分批/同边界摘要重整、压缩状态事件、usage 分类及失败回退。
- 第 1 轮 Reviewer 提出的六项 Finding 已修复，并由 Reviewer 确认其中五项通过；剩余 P1 是输出额度仍未与主模型摘要 Token 预算建立充分约束。第 2 轮实施将输出 Token 上限限制在压缩模型可行额度与主摘要预算内，并按 UTF-8 最坏字节数给出保守 Unicode 字符目标；主模型 tokenizer 的实际摘要校验继续作为硬门槛。
- PostgreSQL 双事务取消竞争与启用压缩的 API/Worker/Redis/SSE 联合测试已新增并通过。第 2 轮压缩/Adapter 测试 37 passed，两个新增 PG/Redis 集成测试 2 passed；全仓 mypy 289 files passed。全仓 Ruff 检查通过；全仓格式检查仍报告 3 个与本功能无关的知识模块既有文件格式差异，功能修改文件 format check 通过。
- 第 2 轮独立 Review 已通过，未发现阻断问题。Root 在最终代码上注入隔离 PostgreSQL/Redis 执行全量 pytest：516 passed、3 skipped、28 条既有弃用警告，耗时 39.55 秒；包含新增真实双事务竞争和启用压缩的联合测试。Ruff、mypy 289 个文件、功能修改文件格式检查和差异检查通过。
- Root 已完成后端验收。真实外部 LLM 未联调，确定性模型测试不代表供应商兼容性或实际摘要质量验证；本次没有提交、推送或部署。临时 PostgreSQL/Redis 容器在验收后清理。
