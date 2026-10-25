# 长期记忆冲突处理改造方案

日期：2026-10-25。状态：设计稿，尚未实施业务代码或数据库变更。

## 1. 现状与目标

当前代码存在以下问题：

| 位置 | 问题 |
| --- | --- |
| `application/memory/extraction.py` | 将事实拼成正文；只比较同 session 最近 50 条记忆；程序预选目标，模型只决定动作 |
| `prompt/memory_resolution.md` 与 `MemoryResolution` | prompt 要求 relation，但程序没有保存；不确定或模型失败时可能按同 key 覆盖 |
| `application/memory/workflow.py` | UPDATE/SUPERSEDE 行为相同；先判定全部候选再写入；幂等键依赖写入计数，重试可能错位 |
| `memory_repository.py`、`memory_worker.py` | 修订缺少并发保护；候选没有持久快照；处理时间代替原始陈述时间；新版本继承旧有效性 |
| `memory/retrieval_service.py` | 跨 session 召回，却不补查冲突另一侧；裁剪和窗口合并不识别事实组 |

目标行为：重复事实只增加证据，多值偏好允许追加，明确更正产生新版本，未解决矛盾保留双方并在必要时澄清。所有变更可追溯，重试不漏写，并发不产生多个当前版本。

首版沿用实例共享记忆，区分 global/session 适用范围；`session_id` 只表示来源。主体不清的项目事实限制在当前 session 或暂缓处理，不猜测项目 ID。多用户身份隔离、完整历史时点查询和定时切换后置。

## 2. 事实与决策

### 2.1 结构化事实

保留正文 `content` 用于展示和 embedding，新增明确类型，停止从正文冒号恢复新事实结构：

| 字段 | 含义 |
| --- | --- |
| `scope_kind / scope_id` | global/global 或 session/当前会话 ID，由程序约束 |
| `subject_key` | `user:self`、有来源支持的 `project:athena` 等主体 |
| `fact_key / fact_value / polarity` | 规范属性、原子值、affirmed/negated；集合拆成独立成员 |
| `claim_intent` | assert/correct/retract，须有原文证据 |
| `evidence` | 用户消息 ID、引文、原始时间；采纳方案须附用户确认及被确认内容 |
| `valid_from / valid_until` | 明确的适用时间，不由模型猜测 |
| `confidence` | 提取可靠度，沿用 0.6 下限，不作为事实真值概率 |

新增小型 `fact_policy.py`，维护规范 key、确定的别名和取值规则：single 属性的不同当前值可能冲突；set 属性允许不同成员并存，只将同成员的肯定/否定视为冲突；没有规则时为 unknown，禁止仅凭 key 相同覆盖。

例如，“喜欢苹果”和“也喜欢梨”并存；“喜欢”不能升级成“最喜欢”。旧 `preferred_fruit` 语义不足时不强制归类。模型建议的别名不自动写入全局规则。

### 2.2 四种动作、两种关系

| 动作 | 条件与结果 |
| --- | --- |
| `IGNORE` | 等价且无待解决冲突；关联已有记忆，保存本次证据，不重复创建 |
| `CREATE` | 独立事实、集合追加或未解决矛盾；矛盾时附 contradicts 目标 |
| `REVISE` | 明确更正/改变/撤回，且目标、时间和证据完整；创建新 revision，旧目标 superseded |
| `DEFER` | 主体、关系、时间或目标范围不确定；保存待确认候选，不作为确定事实使用 |

关系只实现 `supersedes`、`contradicts`。重复和支持证据保存在操作记录中，不增加相同 active 事实，也不按重复次数提高置信度。

固定结构使用严格 Pydantic 模型。模型通过既有 `invoke_structured` 返回 `MemoryResolutionProposal`；程序校验后生成可提交的 `MemoryResolution`，附候选身份、目标版本和状态指纹。各动作只允许自己的字段：IGNORE 必须有等价目标，REVISE 必须有目标列表和证据，DEFER 必须有原因。

目标必须来自实际查询、同主体和适用范围，仍 active 且有效。作用域、来源时间、ID 和最终状态由程序确定。语义不确定使用 DEFER；非法输出、模型超时属于技术失败，保留 pending 并重试，不能退化为覆盖或空候选。

### 2.3 判定顺序与边界

1. 校验原文证据，对本批候选去重并识别更正链；未被用户采纳的助手建议不形成事实。
2. 按 scope、subject、规范 key 跨来源会话精确查询，再用确定别名和有界向量查询补充；同时查询矛盾关系。
3. 使用规则处理无旧事实、等价重复和集合追加；只有需要语义判断时调用 Resolver。
4. 明确更正才允许 REVISE；确认矛盾但不能替代时 CREATE + contradicts；关系本身不清时 DEFER。

提供给模型的目标默认最多 20 条，同时返回实际总数。超限使用 `DEFER(target_set_too_large)`，不针对截断列表修订。invalid、过期、superseded 记录只作历史参考，不进入默认修订目标。

必须保留以下边界：

- A/B 已冲突，用户确认 A：仍需解决 B，不能因 A 等价而直接 IGNORE。
- 更正为 C 时处理完整互斥目标集；新版本沿用主目标 logical_memory_id，其余旧链通过 supersedes 关系关联。主目标优先选等价值，否则按 `(logical_memory_id, id)` 稳定选择。
- 同轮矛盾不能只按数组顺序覆盖；较老作业、未知来源时间或同时间不同值不能靠处理先后决定真值。
- 未来生效的替换不立即 supersede 当前事实；需要定时切换时暂缓处理。

引文、范围、时间和状态可作确定校验；更正意图仍可能被模型误判，须保存 reason_code 和证据供复核，并用真实案例评估。

## 3. 数据模型

| 位置 | 变更 |
| --- | --- |
| `memories` | 增加 scope、subject、fact_key/value、polarity、asserted_at、valid_from、normalization_version；旧结构化字段可为空。实体/API 补齐已有 logical_memory_id、revision、confidence |
| 新表 `memory_write_operations` | 保存 operation_id、turn_id、来源 session、parent_operation_id、事实索引、candidate_json、decision_json、状态、result_memory_id、投影状态和时间 |
| 新表 `memory_relations` | 保存版本两端、kind、operation_id、创建时间、解决时间/操作及原因；两端外键指向具体版本 |
| `memory_processing_jobs` | 增加 source_message_id、source_observed_at、extraction_manifest_json；清单只保存候选 ID 与版本，候选正文仅存 operations |

operations 状态为 pending/applied/ignored/deferred/rejected；投影状态为 not_required/pending/ready/failed。已完成候选、决策和结果 ID 不覆盖；人工复核创建关联新操作。清单 NULL 表示尚未提取，空列表表示成功提取但无候选。

新增约束：

- scope、subject、fact_key 联合查询索引；`(logical_memory_id, revision)` 唯一。
- 每个 logical_memory_id 最多一个未删除的 active 版本；不能对整个属性组限制唯一 active，多值和矛盾需并存。
- supersedes 有方向；contradicts 两端按 ID 排序去重，禁止自关联。
- 新写入结构化字段成套校验；稳定 JSON 直接按具体模型读写，非法新快照报错。开放 metadata 保持扩展能力。

来源时间优先取用户消息 timestamp，其次 Run.created_at，无法追溯则标 unknown。created_at 表示入库时间，asserted_at 表示版本依据的断言时间，last_observed_at 表示最新支持证据时间。时间使用 UTC；校验 valid_until 晚于 valid_from。

新版本采用新候选的证据、confidence 和合法有效性，不机械继承旧 invalid 或期限。重复陈述仅以 max 更新 last_observed_at，不改变 asserted_at，不延长 TTL/事实期限。关键词、向量、候选匹配、窗口核验和访问统计共用有效性条件，排除无效、到期和尚未生效事实。

关系以 PostgreSQL 为事实源；首版正确性不依赖 Neo4j。矛盾边仅在未解决且双方当前有效时影响回答，一侧到期不等于用户确认了另一侧。

## 4. 写入、恢复与并发

流程：领取作业 → 保存提取清单 → 逐候选查询与判定 → 短事务提交事实/关系/回执 → 事务外构建 embedding。

**提取与恢复：**自动 operation_id 由 turn_id 和规范候选内容指纹生成，不含处理时间、模型理由或 written 计数。首次成功提取时，候选与清单原子保存；重试使用冻结清单，已完成项返回原回执，pending 项继续处理。后一个候选必须看到前一个已提交结果，避免先判定整批再写入。

**提交边界：**

1. 事务外读取事实与关系，计算状态指纹，执行规则或调用模型。
2. 短事务内先检查已有回执，按稳定顺序获取事实组 advisory lock，再锁 operation 和目标行。
3. 重查完整事实组和关系；指纹变化则退出事务重新判定，最多 3 次后交给作业重试。空组首次 CREATE 也须复核。
4. 校验目标、证据和原始时间；先 flush 旧 superseded 状态，再插入新 active 版本，同步保存关系和回执。
5. 提交后调用 embedding；网络调用不持有数据库锁。

所有修订、有效性、删除和人工解决入口共用上述边界，避免管理入口绕开保护。指纹不包含 access_count 等无关访问统计。

DEFER 是已处理结果；技术错误保留 pending，由现有 max_attempts 重试，耗尽后关联 failed job。`MemoryProcessResult` 分别返回创建、修订、忽略、暂缓、拒绝和投影失败数量，不能把失败显示为“没有信息”。

embedding 失败保留事实和回执，标记投影失败；重试或显式修复只补投影，跳过已被替代目标。当前终态事务入队和 `RunExecutionService._enqueue_memory` 两条路径都须传递相同的真实来源信息。

## 5. 冲突感知检索

复用关键词、向量和 RRF，将命中结果作为种子，批量补查同事实组及矛盾关系。single 按 scope/subject/key 分组，set 按同成员的肯定/否定分组。即使旧数据缺少关系，结构化互斥值也应标记冲突。

每组生成一个 ContextItem，content 明确包含各方、来源时间和未解决状态。默认最多展开 6 条，超过时标明总数和未完整展开；不能从截断结果推断不存在其他值。

- 使用严格的 `MemoryContextMetadata` 保存 group_key、真实成员 ID、冲突和完整性；source_id 使用真实锚点 ID。
- 预算按整组保留、压缩或淘汰，不允许只留冲突一侧；正文、规划及模型适配器的后续裁剪保持该边界。
- prefetch/directed 合并按 group_key 替换旧快照。进入模型前统一核验，覆盖期间修订和旧 checkpoint；失败移除无法保证一致的记忆。
- 关系补查共用现有阶段预算，不增加读取 LLM；最终核验新增 1 秒默认超时，失败/超时明确记录，不冒充 empty。
- 轨迹按真实来源记录；result_count 计上下文条目，trace.selected_count 计真实选中来源，另记组数。confirm_selection 仅给进入 Root 窗口的实际成员计访问，下游再次淘汰的资料另记实际使用集合。

沿用 Root 已有冲突规则：当前用户明确更正优先；未解决冲突影响答案时才澄清；不按新旧、得分或重复次数擅自选真值，不附带无关冲突。最终核验后的新变更在后续请求可见，不承诺生成期间全局串行一致。

## 6. API 与管理页面

保留已有读取接口，响应补充结构化事实、版本、confidence 和 conflict_count；新增以下接口，使用明确的请求/响应模型：

| 接口 | 用途 |
| --- | --- |
| `GET /api/memory/{id}/relations` | 查看替代与矛盾关系 |
| `GET /api/memory/conflicts` | 分页查看当前未解决冲突组 |
| `GET /api/memory/operations`、`/{id}` | 查看候选、证据、决策及处理失败原因 |
| `POST /api/memory/conflicts/resolve` | 选择“以这条为准” |
| `POST /api/memory/operations/{id}/review` | 对待确认项重判或丢弃 |

静态路径在 `/{memory_id}` 前注册。人工解决请求包含幂等键、group_key、winner_memory_id、期望状态指纹及说明；校验后以用户确认作为新证据生成 revision，替代本互斥组旧值并关闭冲突，不影响其他合法集合成员。

状态变化或同幂等键对应不同请求返回 409；相同请求返回原回执。范围不同的事实须重新结构化，不能直接强行关闭矛盾。

复核创建带 parent_operation_id 的新操作；retry 预建独立作业与清单，直接重判，不重新提取。复核与旧 worker 共用父操作锁；人工接管后旧 pending 不得继续写入，若事实已经提交则返回 409。原 deferred 证据不覆盖，页面按有效后续结果计算待确认项。

现有 MemoryView 增加“记忆 / 存在冲突 / 待确认”，复用详情与版本历史；区分事实矛盾、尚未确认和处理失败。确认依据真实回执刷新，409 刷新事实组并保留用户说明。其他内容修正可通过正常对话进入同一写入流程。

## 7. 实施与存量升级

| 阶段 | 工作与主要代码位置 |
| --- | --- |
| P1：事实与决策 | `domain/memory/entities.py`；新增 `contracts.py`、`application/memory/fact_policy.py`；更新提取/判定 prompt |
| P2：持久化与并发 | `models/runtime_models.py`、导出与仓储；操作/关系模型、候选清单、唯一约束和统一提交 |
| P3：写入接线 | `memory/extraction.py`、`workflow.py`、ports、worker、两条 Run 入队路径、bootstrap；同步响应转换与 fake |
| P4：读取保护 | `memory/retrieval_service.py`、pgvector、context contracts/service、Root 窗口、正文、prompts、settings/bootstrap |
| P5：管理与升级 | `query_controller.py`、记忆服务/响应；前端 client/types/MemoryView；升级脚本和文档 |

P1–P5 构成完整交付，只有写入通过不能直接上线。MemoryModel 实际位于 runtime_models.py，memory_models.py 只是重导出，无须为本次工作额外拆分模型文件。

当前 create_all 不升级已有表。拟提供一次性 `backend/sql/manual/memory_conflicts_v1.sql` 和 `backend/scripts/memory_conflicts.py`，支持 audit/backfill/repair-projections，默认 dry-run，显式 apply 才写入；不恢复通用迁移框架。

升级步骤：

1. 备份并暂停记忆写 worker；审计多 active、重复 revision、非法时间、缺失来源和未知 key。
2. 添加兼容列、新表和普通索引；只回填能从原文和来源确认的结构，无法确认的保留 legacy 标记。
3. 生成冲突与分叉修复清单；不得按时间静默删改。不同值分支须明确选择或拆链并保留冲突，约束建立前再次审计为零。
4. 添加唯一约束，部署新读写路径，恢复 worker，再修复失败投影。
5. 核对事实、来源和版本；分别报告已结构化与未完成回填的范围。

回填不改旧正文，无须全部重建 embedding。回退先暂停写入并保留新 schema；旧 worker 不理解冲突关系，不能直接恢复。完整恢复旧版本需使用升级前备份并核对新增事实。

同步修正 `athena-memory-retrieval.md` 中与当前代码不符的图关系、投影回滚和摘要描述。

## 8. 验收标准

| 类别 | 必须验证的结果 |
| --- | --- |
| 事实语义 | 跨会话重复仅增证据；苹果/梨并存；喜欢不升级最喜欢；否定苹果不影响梨 |
| 更正与矛盾 | 更正产生正确 revision；无替代证据保留冲突；A/B 后确认 A 仍处理 B；不跨主体/范围修改 |
| 判定失败 | 非法输出/伪造目标不改旧事实且可重试；语义不清、目标超 20 条时 DEFER |
| 时间与有效性 | 迟到、同时间和未知时间不靠处理顺序覆盖；invalid、过期、未来生效、superseded 不作当前事实 |
| 恢复 | 首条写完崩溃、提交回执丢失、提取结果变化均不重复/漏写，不出现正文与 embedding 错配 |
| 原子性与并发 | 版本/关系/回执全成全败；首写竞争重新判定；同链最多一个 active；同批候选互相可见 |
| 投影 | embedding 失败保留事实并可修复；修复不新增事实 |
| 检索与窗口 | 单边命中补齐另一侧；预算不截成单边；两阶段和 checkpoint 核验替换旧快照；超限不声称完整 |
| 统计 | 来源与组数区分，访问只计 Root 窗口实际成员；超时与 empty 区分，记录新增延迟 |
| 人工处理 | 同请求同回执；过期状态 409；复核接管后旧 worker 不再写入 |
| 存量升级 | dry-run 报告脏数据和分叉；无静默删除；完成修复后才创建约束 |
| 真实回答 | 跨会话更正、多值、未解决冲突、当前消息优先和无关冲突不外泄；固定案例每例至少 3 次，报告实际通过率 |

规则与类型用定向单元测试；事务、锁、约束和崩溃恢复用真实 PostgreSQL 集成测试；API/前端验证管理操作，真实模型验证回答行为。复用既有记忆写入、召回、持久化和 Run 恢复用例；fake 测试不替代数据库或真实模型验收。

观测复用操作记录和 retrieval trace，记录 action/reason、待确认/失败数、快照冲突、投影失败及延迟，不重复散存原文。

可从 P1 开始实施；发布前仍需审计存量数据库并确认 PostgreSQL、embedding 和真实 LLM 的验收条件。本次只精简方案，未执行实现验收。
