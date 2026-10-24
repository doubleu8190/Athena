# 召回分析 Review 与重新设计

日期：2026-10-23（Asia/Shanghai）。

本方案已按当前修复范围精简。详细文件改动、写入规则、实施步骤及验收标准见[召回分析功能实施文档](retrieval-analysis-implementation.md)；具体实施以该文档的细化规格为准。

以下问题和数据库证据来自代码改造前的审查。当前工作区已有按本文件和实施文档实施的代码改动。审查时的数据库写入复现只使用会话临时表，结束后回滚。

## 1. 结论与实测证据

以下数据和实验来自 2026-10-23 的上一轮审查，不表示本轮重新查询了数据库。问题成立，而且不仅是 candidate 数据少。当前实现同时存在非空轨迹写入失败、过程信息丢失、成功状态失真、API 字段丢失和注入语义错误。

### 1.1 审查时的数据库快照

| 检查项 | 结果 |
| --- | --- |
| `retrieval_runs` | 48 行 |
| `retrieval_candidates` | 0 行 |
| 48 条 run 的状态 | 全部 `succeeded` |
| 48 条 run 的 candidate/selected/injected 总数 | 全部为 0 |
| provider 配置 | 47 条仅 `conversation`，1 条 `conversation,memory` |
| 已持久化的 memory 成功事件 | 5 条，`retrieval_run_id` 全为空 |

事件证明真实非空召回曾经发生。例如 2026-10-23 14:06:36（北京时间），memory 事件报告 `candidate_count=21`、`result_count=5`，但 `retrieval_run_id=null`。所以不能把数据库中的 0 行解释为“从未找到记忆”。

另外，`conversation` 不在当前 bootstrap 装配的四种检索 Provider 中。历史事件里该通道为 `skipped`，但 run 被记为成功。当前 Root 已通过显式 `ContextRequest` 发起查询，不能据历史 conversation 记录断言当前 Root 仍有同样的路由问题。

### 1.2 PostgreSQL 临时表复现

复用真实 ORM 模型和 `PostgresRetrievalRepository.record_context()`，在与业务表结构一致、带即时外键的临时表中运行：

1. 空 `items`：run 可以写入。
2. 一个 memory item：失败，SQLSTATE `23503`。
3. 捕获到的首条 INSERT 是 `INSERT INTO retrieval_candidates`，此时父 run 尚不存在。
4. 对照实验先 `session.add(run)`、`await session.flush()`，再添加 candidate：写入成功。
5. 所有实验写入都发生在 `pg_temp` 临时表，外层事务最终回滚。

实际错误：

```text
IntegrityError / SQLSTATE 23503
insert or update on table "retrieval_candidates" violates foreign key constraint
Key (run_id)=(...) is not present in table "retrieval_runs".
```

这条故障链解释了为什么留下的几乎都是无候选的 run：非空写入会连同父 run 一起回滚，随后应用吞掉异常，业务检索继续，事件中的轨迹 ID 变为 null。

## 2. Review 发现

### P1：非空轨迹事务因父子写入顺序失败

证据：`src/infrastructure/persistence/postgres/repositories/retrieval_repository.py:80`。

run 和 candidates 仅依次调用 `session.add()`，未显式 flush 父记录，也没有声明用于 ORM 工作单元排序的父子 relationship。实际 SQL 顺序并不由 Python 的 add 顺序保证，真实 PostgreSQL 复现了候选先插入而触发外键失败。

`src/application/runs/context/service.py:174` 又以 `except Exception: return None` 静默吞掉该错误。数据库没有轨迹，事件仍可显示业务召回完成，排查时没有错误类型和阶段。

### P1：只保存最终条目，原始候选和淘汰链路无法恢复

证据：`src/application/runs/context/contracts.py` 的 `ProviderResult`，以及 repository 的 `for rank, item in enumerate(items, 1)`。

- Provider 只返回最终 `items` 和数字统计，没有原始候选轨迹。
- keyword/vector 的来源、原始排名、分数及融合前后变化都在返回前丢失。
- `_build_bundle()` 再执行条目数和 token 裁剪，只有剩余 items 进入持久化。
- `candidate_count` 统计通道命中数之和，同一来源在两路命中会重复计数；candidate 行却是预算裁剪后的结果，二者不是同一口径。
- 写入的 `fused_rank` 是全局 items 枚举顺序，未必是融合排名；文件结果没有在 metadata 中携带统一 `score`，导致融合分数可被写成 0。

即便修好外键，也只能看到最终结果，无法分析“某个来源在哪一阶段被淘汰”。

### P1：查询响应与前端契约断裂

证据：`src/application/retrieval/query_service.py:24`、`src/application/common/responses.py:234`、`frontend/src/types/index.ts:433`。

repository 返回 `content_preview`、`fused_score`；应用转换却读取 `content`、`score`，缺省为 `""` 和 `0.0`。响应模型又去掉 candidate ID、排名、选中/注入状态，以及 run 的统计、时间、配置和关联字段。

使用一条完整的合成 repository 记录实际调用 QueryService，可复现：

```text
候选变成：{source_id: m1, content: "", provider: memory, score: 0.0, metadata: {}}
run 缺失：candidate_count, selected_count, injected_count, created_at
candidate 缺失：candidate_id, content_preview, fused_score,
               selected_for_result, injected_into_context
```

Controller 的 `asdict()` 无法恢复这些字段，TypeScript 类型声明也无法验证服务端实际返回的 JSON。因此仅修数据库不会使页面恢复完整。

### P1：把“召回结束”提前记成“模型已使用”

证据：repository 的 `selected_count=len(items)`、`injected_count=len(items)`，以及 `src/application/orchestration/root_workflow.py:385` 的 `_merge_context_window()`。

轨迹记录发生在 Root 窗口合并、跨轮去重和最终模型消息构造之前；这些步骤还会丢弃条目。相同资料也可能被后续多个决策调用重复携带。当前布尔字段无法解释实际是哪次模型调用使用了哪些候选。

文件 Provider 还把 chunk ID 替换成 attachment ID（`src/application/files/retrieval_service.py:129`），窗口按 `(provider, source_id)` 去重时，同文件不同 chunk 会发生身份冲突。记忆访问统计则在 Provider 内就更新，早于全局窗口裁剪。

### P2：运行状态和筛选统计失真

- repository 无条件写 `status="succeeded"`，忽略 failed、timeout、skipped 和 partial；duration 留空，Provider 错误也没有保存。
- 取消路径只发事件，不保存取消轨迹；进程中断没有可恢复的 running 记录。
- `list_runs()` 忽略 `session_id`、`agent_run_id` 和查询文本过滤；`total` 没有应用任何过滤条件。
- `scope` 全部写成 `context`，但页面筛选和标签主要面向 memory/file/knowledge，混合调用没有一致定义。

## 3. 检查过度设计后的实施决定

原设计把候选分析扩展到了任务恢复和模型调用审计，超出了当前修复所需范围。以下内容已从本次实施中删除：

- 新增 retrieval_provider_runs、retrieval_context_uses 两张表，以及逐次执行和使用历史。
- 模块和模型执行的租约、心跳、执行权编号、后台扫描和恢复任务。
- LLMPort/SDK 回调、模型重试关联、固定模型请求 ID 和累计使用计数。
- 完整输出恢复快照、通用版本身份、UUID5 重放协议和严格一次访问计数。
- 通用记录服务、十二个保存操作、逐阶段数据库回调和多项记录策略配置。
- 尚无实际规模需求的模型使用接口、候选分页和多个独立详情面板。

完整候选、正确排名和淘汰原因仍然需要保留；删除上述机制不是把修复缩减为只加一行 flush。

## 4. 本次采用的简化方案

| 方面 | 实施决定 |
| --- | --- |
| 数据表 | 沿用 retrieval_runs、retrieval_candidates，不新增表或列 |
| 模块状态 | 按固定数据结构保存在现有 config_json，包含查询、状态、数量、耗时和错误 |
| 候选过程 | 每个来源一条候选，已有 metadata_json 中保留两路命中和最终选择状态；真实排名、预览、定位等使用已有列 |
| 写入 | 检索模块在内存保留过程，采集结束时一次事务写 run 和全部候选；先 flush 父记录 |
| 全局筛选 | 保持现有 _build_bundle 和窗口合并规则，只补充实际选择和淘汰记录 |
| 最终选择 | Root 合并后，通过一次短事务确认本次新候选的保留情况；未确认时显示未知 |
| 模型使用 | 本版不审计实际 SDK 发送；页面改显示“本轮保留”，接口不再提供注入字段 |
| 记忆访问 | 将已有 record_access 调用移到本轮最终选择后；批次内去重，不增加跨进程严格一次机制 |
| ID 和恢复 | 复用本次调用已有 invocation_id 防止相同 ID 重复保存；Root 真正重新查询可产生新记录，不接管原恢复逻辑 |
| 接口与页面 | 修复现有列表、详情接口与候选表格，不新增使用历史页或接口 |
| 故障 | 保存失败记录日志和事件，不改变回答；不保证强制退出后能恢复未保存候选 |
| 历史 | 按开发阶段处理，不增加历史格式适配或数据回填 |

固定 JSON 结构仍需明确类型和校验，不能为了不加表就把稳定字段变成无约束字典。详情候选由现有检索上限约束，只有实际测量发现过慢时才另行考虑分页。

详细字段、文件改动、写入顺序和验收要求见[召回分析功能实施文档](retrieval-analysis-implementation.md)。代码进度和实际验证状态以工作区当前改动为准。

## 5. 现有证据与后续验收

本次审查已完成的验证：

- 真实数据库只读聚合和召回事件核对。
- PostgreSQL 临时表中复现非空写入外键错误，并以父记录显式 flush 对照验证。
- 通过真实 QueryService 复现接口字段丢失。
- 相关现有测试 24 项通过；它们没有覆盖上述真实父子事务，不能视为新功能已完成。

实施后仍须验证：非空事务、全部候选与命中、真实排名、两次全局筛选、准确状态、完整 API 字段、筛选总数、记忆访问位置，以及真实浏览器页面。

任务恢复、模型逐次使用和跨进程严格一次计数已移出本次范围，不能继续作为本次验收的前置条件。以后有明确需求时单独设计。
