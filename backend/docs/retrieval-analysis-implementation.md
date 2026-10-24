# 召回分析功能实施文档

日期：2026-10-23。状态：已完成方案检查，尚未修改功能代码。

适用仓库：`/Users/udouble/Documents/code/Athena`。文件路径均相对此仓库。

本文根据[召回分析问题检查](retrieval-analysis-redesign.md)编写。本版已删除超出当前需求的设计，以修复候选记录、统计、接口和页面为主。原实施方案中与本版不一致的要求不再执行。

## 1. 当前现状

### 1.1 几个名称的含义

| 名称 | 含义 |
| --- | --- |
| 召回 | 从记忆、附件、知识库或图谱中查找与问题相关的资料 |
| 候选 candidate | 找到的资料，包括最后保留的和被淘汰的 |
| 检索模块 Provider | memory、file、knowledge、graph 四种检索代码 |
| 检索方式 channel | 模块内使用的查找方法，例如关键词 keyword、向量 vector |
| 资料窗口 | Root 当前保留、准备供后续模型调用使用的资料 |
| token | 模型计算文本长度的单位，用于限制资料长度 |
| 过程记录 trace | 查询、命中、排名、筛选结果和错误等分析数据 |

### 1.2 当前流程和已发现的问题

当前流程：

```text
Root 决定查询
  → ContextPlanner 检查资源范围
  → ContextAcquisitionService 调用各检索模块
  → 模块只返回筛选后的资料及数量
  → _build_bundle 按数量和长度删减
  → record_context 保存召回和剩余条目
  → _merge_context_window 与旧资料再次合并、筛选
  → Root 后续构造消息并调用模型
```

已有 `retrieval_runs` 和 `retrieval_candidates` 两张表，但代码没有正确使用：

| 问题 | 影响 |
| --- | --- |
| 候选先插入，所属召回记录尚未插入 | 非空结果触发外键错误，整次保存撤销 |
| 保存错误被忽略，没有日志说明 | 回答继续，但分析数据消失 |
| 只保存最后剩下的条目 | 原始候选、通道排名和淘汰原因丢失 |
| 查询返回结构缺字段、字段名读错 | 预览变空、分数变 0，统计和状态到不了页面 |
| 召回完成就标记“已注入” | 后面仍会删减资料，无法证明它们实际交给了模型 |
| 文件片段使用文件 ID | 同文件不同片段被错误去重 |
| 所有运行均记成功，部分筛选不生效 | 无法准确解释失败和分页总数 |
| 记忆在模块返回前就增加访问次数 | 最后被全局筛选淘汰的记忆也可能被计数 |

2026-10-23 上轮检查的数据库中，有 48 条召回、0 条候选；同时存在有结果的 memory 事件，但关联的 retrieval_run_id 为空。这是当时的快照，验收时要重新查询。

真实 PostgreSQL 临时表实验已确认：空结果可写入；非空结果先写 candidate 并触发 23503；先 flush 召回记录再写候选可成功。实验最终回滚，没有改业务数据。

上一轮 24 项相关测试通过，但使用了模拟数据库访问或空候选，未覆盖这个真实写入问题。

### 1.3 原方案哪里设计过多，如何删减

| 原方案要求 | 为什么超出当前需要 | 本版处理 |
| --- | --- | --- |
| 新增模块执行表、模型使用表 | 当前没有查询多次模块重试或逐次模型发送的明确需求；模块状态可以随召回保存 | 保留现有两张表，模块摘要放 config_json |
| 租约、心跳、执行权编号和后台恢复任务 | 相当于再建一套任务调度；现有 Root 已负责执行和恢复 | 全部删除，分析记录不接管任务执行 |
| 修改 LLMPort、SDK 重试、格式修复调用 | 将候选分析扩成模型请求审计，改动范围过大 | 不改模型调用接口；页面只报告最终资料选择 |
| 固定模型调用 ID、UUID5、请求摘要、多版本候选和当前执行切换 | 为严格重放和历史审计增加了多套身份与统计 | 复用本次调用已有 invocation_id，只防止同一次保存重复插入 |
| 每完成一个检索方式就写数据库、完整结果恢复快照 | 增加写入步骤并与业务恢复耦合 | 模块在内存保留过程，采集结束统一保存 |
| 12 个保存方法、新记录服务和回调接口 | 分层数量超过实际保存动作 | 沿用 Repository，保留一次保存和一次选择结果更新 |
| 新增模型使用分页、候选分页和多个详情面板 | 当前候选已有 recall_limit 等数量上限，尚无明细过大证据 | 沿用两个现有接口和候选表格 |
| 8 个记录配置、专用重试策略和访问统计事务锁 | 为未要求的可靠性目标增加配置和运行维护成本 | 不新增策略框架，只保留必要错误日志和短超时 |
| 改变全局筛选算法、统一消息渲染和 tokenizer | 修复分析数据不需要同时重做模型输入流程 | 保持现有筛选顺序和规则，补记各步的选择结果 |

保留完整候选、真实排名、淘汰原因、明确的数据类型和真实数据库测试是必要改动，不因精简而删除。

## 2. 需求概览

### 2.1 本次必须做到

1. 非空召回能保存，召回和候选在同一事务内一起成功或撤销。
2. 在现有检索数量限制内，保存全部实际获得的候选，而非只保存最终结果。
3. 能看出候选来自哪些检索方式、原始排名分数、合并排名，以及淘汰原因。
4. 能区分“模块选中”和“本轮最终保留在资料窗口中”。
5. 接口和页面保留来源、定位、排名、状态、错误和正确统计，筛选总数准确。
6. 保存失败有说明，业务回答可以继续；本轮最终选择未确认时明确显示等待确认。
7. 以真实数据库、接口和浏览器操作证明修复有效。

### 2.2 本次不承担的能力

本次不建设模型请求审计，不证明远端收到或使用了资料；不记录每次模型重试的资料，不统计累计模型使用次数。

不增加任务调度、心跳、后台回收、完整结果恢复和跨进程严格一次计数。Root 从已有保存点继续运行的方式保持原样。如果 Root 确实重新执行一次查询，可以留下新的召回记录，不将其强行合并为同一次执行。

不改变检索权限、记忆跨会话范围、图查询上限、已有排序策略和重排启用情况。

## 3. 目标

### 3.1 页面要能回答什么

用户应能从一次召回详情看清：查了什么、各模块执行得怎样、查到了哪些候选、如何排名、哪些被淘汰，以及哪些被本轮最终资料筛选保留。

**“进入资料窗口”与“模型已使用”分开。** 本版只确认前者，删除页面中把前者显示成“已注入”的做法。

### 3.2 统计怎么算

| 字段 | 含义 |
| --- | --- |
| raw_hit_count | 所有已获得通道结果的命中次数，同一资料关键词和向量各命中一次算 2 次 |
| candidate_count | 在每个模块内按来源合并重复命中后的候选数，包括后续淘汰者 |
| selected_count | 各模块排序筛选后输出的数量，尚未经过全局资料筛选 |
| context_count | 本次新资料经过 _build_bundle 和 _merge_context_window 后最终保留的数量，不重复计算以前的资料 |

正常完成且记录完整时：

```text
raw_hit_count >= candidate_count >= selected_count >= context_count
```

模块失败或取消时，能保留下来的数量只表示“已经获得并记录的结果”，页面应结合错误说明，不能解释成成功检索后的全部结果。

最终资料筛选尚未确认或保存失败时，context_count 和 context_selected 返回 null，不能当作 0 或 false。

injected_count 和 injected_into_context 不再作为本功能的有效统计。接口不返回这两个字段，前端删除对应卡片、筛选和“已注入”标记；现有数据库列不再写入有效值。

### 3.3 需要保留的状态

- status 说明检索结果：succeeded、partial、failed、timeout、skipped、cancelled，沿用现有状态含义。
- trace_status 说明本次分析记录：complete 已确认最终选择、incomplete 尚未确认或保存失败。可由选择状态计算，不新增数据库状态列。
- selection_status 只说明本轮资料筛选：pending 待确认、complete 已确认、not_run 未进行。

pending 不等于进程仍在运行，程序中断也可能留下待确认数据。页面显示“最终资料选择未确认”，不承诺后台会补齐。

## 4. 要改动哪些地方，怎么改

### 4.1 改动文件

| 文件 | 具体改动 |
| --- | --- |
| `backend/src/domain/retrieval/trace_contracts.py`（新增） | 定义命中、候选过程、模块摘要及固定 JSON 结构，供应用和 Repository 共用 |
| `backend/src/domain/retrieval/query_ports.py` | 沿用 RetrievalPort，明确保存参数，增加更新最终选择的方法；不再拆一套通用记录框架 |
| `backend/src/application/runs/context/contracts.py` | ProviderResult 增加候选过程，ContextBundle 增加本次 retrieval_run_id；新增字段有合理默认值 |
| `backend/src/application/memory/retrieval_service.py` | 返回完整候选和模块输出，保存两种命中及真实排名，不提前计记忆访问 |
| `backend/src/application/files/retrieval_service.py` | 返回完整候选，source_id 改成 chunk ID，保留定位和真实合并分数；知识库包装不丢新字段 |
| `backend/src/application/retrieval/graph_service.py` | 保存已返回的节点或路径候选、空内容淘汰及模块状态 |
| `backend/src/domain/retrieval/fusion.py` | 分开计算完整排名和取前 K 条，保持原有得分与平分顺序 |
| `backend/src/application/runs/context/service.py` | 保留所有模块候选，一次保存；记录第一次全局删减；提供最终选择确认入口，暴露保存错误 |
| `backend/src/application/orchestration/root_workflow.py` | 合并资料后确认本轮新资料的最终选择；恢复读取新增的可选关联 ID |
| `backend/src/bootstrap/postgres.py` | 将现有记忆 Repository 传给负责最终选择确认的应用服务，不增加后台任务 |
| `backend/src/infrastructure/persistence/postgres/repositories/retrieval_repository.py` | 修复父子顺序，保存完整候选及 JSON 摘要，更新选择结果，修复筛选和 total |
| `backend/src/application/retrieval/query_service.py`、`backend/src/application/common/responses.py` | 补齐现有响应字段，按明确字段映射，不顺带搬迁所有响应类型 |
| `backend/src/interfaces/http/controllers/query_controller.py` | 保留两条现有路由，补 provider 等实际需要的筛选，返回完整数据 |
| `frontend/src/types/index.ts`、`frontend/src/api/client.ts` | 同步字段、可空统计和筛选条件 |
| `frontend/src/components/pages/RetrievalView.tsx` | 展示模块摘要、通道命中、淘汰原因和本轮保留状态，移除误导的已注入显示 |
| `backend/src/application/runs/query_service.py`、`frontend/src/components/pages/RunChainView.tsx` | 仅展示命中、候选、进入资料窗口和最终保留数量 |
| `backend/tests/` | 在现有测试附近补真实写入、字段、数量、状态和窗口筛选用例 |

不修改 LLMPort、模型 SDK、模型重试和 Worker 调度。保留工作区其他未提交修改。

### 4.2 数据用现有两张表怎样保存

#### 4.2.1 召回表 retrieval_runs

继续使用 query、scope、status、session_id、agent_run_id、message_id、candidate_count、selected_count、duration_ms、created_at、completed_at、error_message。

利用现有 config_json，增加以下固定结构，采用 Pydantic 校验读写，不让结构变成任意字典：

```text
trace_version: 2
raw_hit_count: 已获得的总命中次数
providers: 各模块摘要列表
  - provider、实际 query、资源范围和实际生效的参数
  - status、duration_ms、error_message
  - 各检索方式状态、命中数、候选数、模块选中数
selection_status: pending / complete / not_run
context_count: 本次最终保留数；未确认时为空
```

scope 继续是 context；用独立 provider 条件筛选参与模块。模块通常最多四个，摘要随请求一次读取，不需要单独建执行表。

新记录统一写 trace_version=2。实际参数只保存与检索有关的内容，不保存密钥。

#### 4.2.2 候选表 retrieval_candidates

直接使用已有 candidate_id、run_id、provider、source_type、source_id、revision_id、fused_rank、fused_score、rerank_rank、rerank_score、selected_for_result、filter_reason、content_preview、source_title、locator_json、created_at。

在已有 metadata_json 中增加一个固定的 retrieval_analysis 对象，保留原有其他 metadata：

```text
retrieval_analysis:
  hits: [{channel, rank, score}, ...]
  context_selected: true / false / null
  selection_stage: provider / bundle / window / null
```

- hits 保存每种方式的原始排名和分数，同候选两路命中仍是一行。
- selected_for_result 表示模块是否选中，context_selected 表示本次最终是否留在资料窗口，不能混为同一个状态。
- filter_reason 保存首次淘汰原因，selection_stage 说明发生在哪一步。
- 双路命中时 native_rank/native_score 不代替 hits；可返回 null，页面分别展示两路分数。
- 未执行重排时 rerank 字段为 null，不假设执行过。
- 数据库已有的 injected_into_context 列本版不使用；接口不提供“模型已使用”字段。

来源身份沿用模块内真实 ID：memory 用记忆 ID，file/knowledge 用 chunk ID，graph 用节点或路径 ID。存在 revision_id 时保留；不新增通用版本系统、内容摘要 ID 和版本切换逻辑。

同一来源重复命中按已有 ID 合并；若同一 ID 的正文不一致，保留实际采用的正文并记录冲突说明，不声称完成版本追踪。

**本版不需要新增表、列或 SQL 升级文件。** 仅固定已有 JSON 字段的内容格式并补齐代码读写。只有后续测出查询瓶颈，才考虑新增索引或字段。

### 4.3 检索模块怎样保留完整候选

各模块先在内存收集过程，最终通过 ProviderResult 一次返回。不增加 ProviderTraceSink，不让模块直接保存数据库。

ProviderResult 在现有 items、status、error_message、duration_ms 外增加 candidates 和模块实际查询摘要。candidate_count 从去重候选计算，raw_hit_count 从原始通道结果计算，result_count 等于模块输出条数。

ContextAcquisitionService._invoke_provider 当前会重新构造 ProviderResult，必须同时传递新增字段，避免模块已经返回完整过程，却在这一层再次丢失。完整候选只保留在本次采集内存和分析表中；返回给 Root 的保存点仍只带资料窗口和轻量模块摘要，不复制全部候选，也不增加恢复快照。

#### 4.3.1 memory

1. 关键词结果返回后，立即在内存记录其 ID、排名、分数及必要的过滤原因。
2. 向量结果返回后记录对应信息，检查来源是否有效，缺失 ID 不能使用字符串 None 冒充。
3. 合并同来源命中，保留全部合并排名，再标记哪些属于模块最终输出。
4. 输出 ContextItem 继续携带正文和真实来源 ID；分析对象保存预览和排名，不代替业务正文。
5. 后一步失败时，返回已有候选及准确错误，不再将已取得的关键词过程一起丢掉。是否返回可用业务资料仍遵循当前失败策略，不新增自动回退。

#### 4.3.2 file / knowledge

1. 继续限定在允许访问的附件和 READY 知识文档中查询。
2. ContextItem.source_id 改为 chunk.id，文件 ID 放 metadata/locator，避免多个片段被错误去重。
3. 记录关键词、向量各自原始排名分数，以及真实 fused_score。保留标题、页码和其他定位信息。
4. 知识库调用文件检索后，包装结果时保留完整候选和摘要。
5. 不改变现有总数量限制，也不将“总上限”误报成“每文件上限”。

#### 4.3.3 graph 与排序

图谱只记录原查询实际返回的节点/路径；空正文可记 empty_content。没有关键词、向量和 RRF 的阶段显示不适用，不增加图查询深度或数量。

Fusion 需要提供完整的有界排序结果，再按当前规则标记前 K 条。仍用现有 RRF 算法及平分时的顺序，不顺带改变权重、阈值或重排。只在原有检索上限内保留候选，不扩大查询范围。

### 4.4 统一保存怎样修复

沿用 RetrievalPort.record_context，参数改为明确的请求摘要、模块结果和全部候选，不接受 application 的 ContextItem 任意对象列表。共用类型放在 domain/retrieval，避免领域接口反向依赖应用代码。

#### 4.4.1 一次事务写入召回和候选

1. 复用当前调用已有 invocation_id 生成本次 retrieval_run_id，例如 retrieval_{invocation_id}。不增加 UUID5 或跨运行的身份协议。
2. 检索模块返回后，将召回、模块摘要和全部候选在一个短事务中保存。不是只写 _build_bundle 保留的 items。
3. 先 session.add(run)，再 await session.flush()，确认父记录已经插入，然后批量添加 candidates，最后提交。
4. 任一候选写入失败，一起撤销这次召回和候选，记录具体错误；不能留下虚假的成功父记录。
5. 同一次调用重复提交同一 run_id 时，利用数据库主键冲突处理后读取已有结果，不能只靠“先查再插”判断。不增加一套执行权或 attempt 系统，结果已确认时不让旧的待确认数据覆盖它。
6. 不在数据库事务中等待检索、向量计算或模型调用。

数据库主键只能防止同一 ID 重复插入；Root 后续真正重新发起的查询使用新的调用 ID，可形成新记录。本版不承诺恢复后只执行过一次检索。

#### 4.4.2 状态、错误和超时

| 模块最终结果 | run.status |
| --- | --- |
| 有成功，也有失败或超时 | partial |
| 有成功，没有失败或超时 | succeeded |
| 没有成功，实际执行的模块全部超时 | timeout |
| 没有成功，且存在普通失败 | failed |
| 所有模块都跳过 | skipped |
| 调用方取消 | cancelled |

成功但零命中仍是 succeeded；结合候选数显示“没有找到资料”。检索失败不能也显示成成功空结果。

保存错误不能再 except 后直接返回 None。至少记录 run/session/agent_run ID、失败步骤和错误类型，并在现有事件中带 trace_status=incomplete。数据库不可用导致事件也无法保存时，保留日志，不承诺故障记录一定能落库。

记录操作可以使用局部 2 秒超时，避免分析保存拖住回答；不新增通用重试框架和多项配置。不要把这个保存超时用于业务检索，memory 的现有有效超时保持不变。

取消时继续等待模块退出，并尽量保存已经返回的结果和 cancelled 状态，再传播取消。进程被强制结束时，尚未统一保存的结果可能丢失；这是简化后明确接受的限制，不增加后台恢复系统补造结果。

### 4.5 最终资料选择怎样确认

保留现有 _build_bundle 和 _merge_context_window 的业务行为，只增加选择结果记录。不要借这次修改切换排序规则、删减顺序或 token 计算方式。

1. _build_bundle 记录哪些模块输出因条数或 token 限制被淘汰。超过 token 限制后原代码会停止处理，后续未再检查的条目标 token_budget_stop，不能说每条都单独超长。
2. _merge_context_window 合并新旧资料时，继续按现有规则处理；返回本轮新资料的保留结果和去重、数量、长度原因。
3. ContextBundle 增加可选 retrieval_run_id，Root 合并结束后调用 ContextAcquisitionService.confirm_selection(run_id, selection)。该入口负责更新分析和本轮记忆访问，不接管模型调用。
4. Repository 增加 update_context_selection，在一个短事务内更新候选 metadata 和 config_json.context_count/selection_status。
5. 通过本次 run_id 及候选真实来源关联更新；只确认本次新候选，不改写旧召回的历史选择，也不统计旧资料被后续模型重复使用多少次。
6. 新关联 ID 通过现有 Root 状态保存和读取；最终选择字段只用于本版新产生的资料窗口。

首次保存候选时 selection_status=pending、context_count=null、context_selected=null；确认后再填写真实结果。若程序退出或更新失败，仍显示“最终资料选择未确认”，不能把待确认当成全部未保留。

本次进入采集服务的调用生成分析记录。资源非法或不可用查询在 Root 处已经被拦截、没有进入采集服务时，沿用当前的错误或跳过说明，不额外为每个前置校验建立一条分析任务。

#### 4.5.1 记忆访问只跟随最终选择

删除 MemoryContextProvider 内提前执行的 record_access。在 confirm_selection 中，从“本次新找到且最终保留”的资料中取 memory ID，去重后调用已有 MemoryRepositoryPort.record_access。

ContextAcquisitionService 显式接收现有 MemoryRepositoryPort，由 bootstrap 传入；不新增访问去重表和事务锁。分析保存失败与记忆访问失败分别记录，不因此改变已选资料或回答。

本版的 access_count 含义为“记忆被选入最终资料窗口的次数”，不是网络发送次数。管理读取、仅命中、被最终淘汰和只是复用旧窗口资料都不增加本轮访问。

一次调用内去重；不新增跨进程严格一次保证，也不保证分析 JSON 和访问计数在独立事务之间完全同步。这两项有独立需求时再设计，不能作为本次修复完成的前置条件。

### 4.6 接口和页面怎样改

#### 4.6.1 沿用两条接口

- GET /api/retrieval/runs：继续列表分页，支持当前已有的 scope/status/session_id/agent_run_id/q，增加实际需要的 provider 筛选。
- GET /api/retrieval/runs/{id}：返回完整摘要、模块信息及当前调用的全部候选。

候选数量受已有 recall_limit、模块数量和图查询上限限制，本次不新增明细分页和使用记录接口。配置调大且真实测量证明详情过慢时，再设计分页，不能为控制响应大小悄悄丢掉候选。

items 和 total 使用同一组筛选条件，先筛选后分页；provider 匹配 JSON 内的模块摘要，不能因多个模块重复返回同一 run。q 按字面文字匹配，转义 % 和 _。列表按 created_at、run_id 倒序，候选按模块和真实排名稳定排序。

模块摘要统一按本版结构保存和读取，前端直接使用结构化字段。

#### 4.6.2 补齐现有响应类型

在现有 RetrievalRunResponse、RetrievalCandidateResponse 中补字段，不移动所有共享响应定义，也不为读、写、页面各复制一套同名类型。

召回响应包含：关联 ID、query、scope、status、trace_status、raw_hit_count、candidate_count、selected_count、context_count、selection_status、模块摘要、实际配置、时间、耗时和错误。

候选响应包含：candidate_id、run_id、provider、来源 ID/版本、标题、预览、定位、hits、融合/可选重排信息、selected_for_result、context_selected、selection_stage、filter_reason 和原有 metadata。

按实际字段映射 content_preview、fused_score，禁止用 content/score 的缺省空值把字段错位掩盖掉。声明完整 HTTP response_model，检查 OpenAPI 与返回数据一致。

接口不提供模型使用次数。最终选择还未确认时，context_count 和 context_selected 返回 null。

#### 4.6.3 复用现有页面

保留列表、详情和候选表格，在现有页面上补：

- 四项主要数量：命中次数、候选数、模块选中数、本轮最终保留数。
- 模块摘要：查询、状态、耗时、错误及各检索方式的结果。
- 候选展开：两路排名分数、融合排名、预览、定位和首次淘汰原因。
- 筛选：全部、模块选中、本轮保留、已淘汰。条件可以包含，不强行分为互斥类别。
- 清楚区分成功无命中、失败、跳过、分析记录不完整和最终选择未确认。

移除“已注入”数量、筛选和徽标，不新增模型使用页、逐次重试详情或资料版本浏览器。切换记录和条件时重置展开状态，避免旧请求响应覆盖新详情。

运行时间线只需补同一组真实统计、缺失说明及详情链接，不重做模型活动系统。

### 4.7 实施顺序

| 步骤 | 工作 | 完成条件 |
| --- | --- | --- |
| 一 | 修复父子写入、错误说明、现有接口字段和筛选 | 真实 PostgreSQL 非空结果保存成功，接口不丢字段 |
| 二 | 模块返回全部候选，保存命中、排名、模块摘要和淘汰原因 | 双路重复命中正确合并，尾部候选仍可查看 |
| 三 | 补记两次全局筛选的结果，修正文件片段身份及记忆计数位置 | 最终保留数与 Root 资料窗口一致，待确认数据明确显示 |
| 四 | 更新原页面和接口类型，执行真实验收 | 第 5 节要求通过 |

不新增 SQL 表结构升级，也不处理历史数据。本版新记录写入 trace_version=2。

只需在现有服务和 bootstrap 中更新参数，不增加新后台进程、新调度任务或部署配置。

## 5. 验收标准

### 5.1 固定样例

关键词找到 A、B，向量找到 A、C。A 是同一来源，合并后按当前排序得到 A、B、C，模块选前两条，最终资料窗口只保留 A。

| 检查项 | 预期 |
| --- | --- |
| 命中次数 | raw_hit_count=4 |
| 候选明细 | 3 行；A 保存两条 hits，B、C 各一条 |
| 模块输出 | selected_count=2；A/B 选中，C 因 provider_top_k 淘汰 |
| 最终资料选择前 | context_count=null、context_selected=null，selection_status=pending |
| 最终资料选择后 | context_count=1；A 保留，B 写明数量或长度限制及实际发生阶段 |
| 模型使用显示 | 不声称模型已使用；接口和页面都不显示“已注入” |
| 记忆访问 | 若 A/B 是记忆，只给最终新保留的 A 增加一次本轮访问 |

### 5.2 必须通过的检查

| 编号 | 检查什么 | 通过标准 |
| --- | --- | --- |
| A01 | 真实数据库事务 | 非空召回和候选能保存，无 23503；故意触发子记录约束错误时同事务全部撤销 |
| A02 | 完整候选 | 所有实际获得的候选均保留，两路命中合并，原始分数排名和真实融合排名可查，明细与计数一致 |
| A03 | 文件和来源身份 | 同文件两个片段 ID 不同且可同时保留；记忆跨会话范围、知识库 READY 及图查询上限不变 |
| A04 | 筛选原因 | 模块前 K 条、_build_bundle 的停止规则和窗口合并原因准确；不为记录方便更改原筛选策略 |
| A05 | 最终选择确认 | 本轮保留数与 Root 实际资料窗口中的本轮新资料一致；复用已有资料不重复计数；未确认或更新失败显示未知 |
| A06 | 模块失败和零结果 | 成功空结果、partial、failed、timeout、skipped 正确区分；后续步骤失败仍能查询此前已获得的候选 |
| A07 | 取消和保存失败 | 取消继续传播，完成部分尽量保留；保存故障有可关联日志/事件且不改变业务结果；不要求强制退出后找回未保存数据 |
| A08 | 接口和筛选 | 真实 repository → QueryService → HTTP 保留所有约定字段；筛选有效、total 一致、列表无重复 |
| A09 | 页面操作 | 候选展开、筛选、定位和错误说明可用；无错误的“已注入”展示，快速切换不串数据 |
| A10 | 记忆访问 | 管理读取、仅命中、被淘汰、旧资料复用均不增加本轮访问；同一批次来源去重；不把原有恢复限制宣称为严格一次 |
| A11 | 范围控制 | 不新增业务表、后台任务、租约、模型调用回调或独立恢复协议；新格式基于已有 JSON 字段 |

### 5.3 如何验证

1. 在现有测试中补字段映射、完整候选、状态和窗口确认用例。可集中增加 test_retrieval_analysis.py，不按每个设计概念强制新建文件。
2. 增加真实 PostgreSQL 集成测试，使用 ATHENA_TEST_DATABASE_URL 的独立测试 schema。必须覆盖外键、原子撤销、相同 run_id 重复提交、JSON 读写和最终选择更新。
3. 更新 test_query_controller.py、test_root_context_contracts.py、test_root_graph.py、test_memory_context_recall.py、test_target_native_features.py 中受影响的断言。移动记忆计数后，不能继续断言模块返回就发生访问。
4. 在 frontend 运行 npm run typecheck、npm run build，并执行 git diff --check。
5. 浏览器实际打开列表和候选展开，至少验证真实 memory 命中、同文件多片段，以及已配置 knowledge/graph 的真实返回和失败说明。按请求 ID 核对数据库、接口和最终资料窗口。

没有测试数据库导致跳过，不算数据库验收通过；构建成功不代替浏览器操作；HTTP 200 不代替候选明细正确。真实依赖不可用时记录具体错误，该项保持未通过。

### 5.4 交付与完成判定

交付代码、受影响测试、更新后的响应类型和页面，并附测试结果、样例 session/agent_run/retrieval_run ID、数据库和接口核对结果、浏览器检查记录。

所有必须检查通过后，才算本次修复完成。不能因为删除了复杂设计就只补一行 flush；完整候选、正确字段、准确统计和真实页面验证仍是交付内容。

逐次模型使用审计、跨进程严格去重、完整恢复快照和大规模明细分页只有出现独立需求或测量结果时再评估，不列为本次验收前置条件。
