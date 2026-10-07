# 记忆、上下文检索与检索轨迹

依赖：[系统实现总说明](athena-system-overview.md)、[运行时与数据底座](athena-runtime.md)。被 [前端复刻](athena-frontend.md) 依赖；文件检索细节见 [文件智能与知识库](athena-files-knowledge.md)。

## 1. 长期记忆模型

记忆记录包含 id、logical_memory_id、session_id、content、embedding、metadata、expires_at、last_accessed_at、access_count、memory_type、category、confidence、source_kind、status、source_turn_id、last_observed_at、validity_status、valid_until、revision 和 deleted_time。状态 active/superseded/deleted；事实有效性 valid/uncertain/invalid 独立于 TTL 和访问热度。

正文在 PostgreSQL，`content_fts` 是中文 FTS 计算列，embedding 在 pgvector，记忆实体/修订/关系在 Neo4j。embedding metadata 保存 provider:model:version:dimension 签名；更换签名会清理旧向量，原始记忆仍保留等待重建。

## 2. 写入工作流

普通完成的 run 在 `process_completed_run` 后异步提交记忆写作业。FactExtractor 用副 LLM 从 user/assistant 对话提取原子事实，包含 content、type/category、confidence、validity；LongTermMemorySummarizer 按 `SUMMARY_THRESHOLD=10` 轮生成摘要。MemoryWriteJobWorker 持久化 job，失败可重试，不阻塞当前回答。

写入必须先在 PostgreSQL 插入 active 记录，再写 vector 和 graph；后两者失败时删除新记录并清理 projection。`operation_key` 用于幂等。

## 3. Revision、有效性和关系

`revise_memory(old_id, content)` 不更新原文，而创建 revision+1 的新记录，保留 logical_memory_id，事务性将旧记录标记 superseded，并建立 Neo4j `supersedes` 边。任何 projection 失败都回滚 active 状态和新记录。

`set_memory_validity()` 只修改事实有效性和 last_observed_at；invalid 记忆保留历史但不参与检索。`valid_until` 必须为 ISO-8601。`add_memory_relation()` 写 supports/contradicts 等关系。过期清理由后台定时任务执行：软删 expired，删除 vector 和 graph，失败恢复 PostgreSQL 状态。

访问统计在内存按 memory id 聚合，`MEMORY_SYNC_INTERVAL` 默认 900 秒 flush 到 PostgreSQL；管理页 get/list 不增加访问热度，真正被上下文选中的结果才记录访问。

## 4. HybridMemoryRetriever

上下文检索按 memory requirement 调用 HybridMemoryRetriever：

1. vector 召回 `RETRIEVAL_CANDIDATE_K`，过滤 inactive、invalid、过期、session 不匹配。
2. PostgreSQL FTS 关键词召回。
3. RRF 融合，向量权重默认 0.75、关键词 0.25，RRF k=60。
4. 计算生命周期/时间衰减/记忆性和 exact match 修正。
5. 可选 cross-encoder rerank，回退融合分数。
6. 按 `RETRIEVAL_CONTEXT_K` 选入 ContextBundle，并记录 selected_for_context。

每次检索创建 retrieval_run：query、scope=memory、session_id、agent_run_id、message_id、index_generation、config、candidate_count、selected_count、status、duration_ms、error_message。候选记录 provider、native/fused/rerank score/rank、logical_source_id、revision_id、filter_reason、content_preview、source_title。

`MemoryRetrievalService.get_context()` 在 token budget 内选择结果；超出数量或 token 时保留高分项，并返回可注入的引用与 retrieval_run_id。异常时允许返回空上下文并把 trace 标记 partial/failed，不能伪造有结果。

## 5. Graph context

知识库文档在处理完成后由 GraphDocumentIndexer 用副 LLM 抽取实体和关系写入 Neo4j；记忆写入也创建 memory 节点。Graph provider 按 query 查最多 `GRAPH_ENTITY_LIMIT=8` 个实体、`GRAPH_PATH_LIMIT=12` 条路径，最多 2 hops，最低置信度 0.65，单次超时默认 5 秒。图失败不阻断文件向量检索，context item 标记错误和 trace 状态。

## 6. 对外 API

- `POST /api/memory/search`：向量直接搜索，支持 `where` 和 n_results。
- `GET /api/memory`：分页列表，可 expired_only 和 session_id，返回总数/过期数/最近新增数。
- `GET /api/memory/{id}`：正文和 metadata，不改变 access stats。
- `GET /api/memory/{id}/revisions`：完整不可变 revision 链。
- `GET /api/retrieval/runs`：分页、scope/status/session/run/query 过滤。
- `GET /api/retrieval/runs/{id}`：运行配置、所有候选和最终注入状态。

## 7. 复刻测试

测试向量/关键词融合和 tie 行为、无效/过期过滤、访问统计 flush、相同 operation_key 幂等、revision supersedes 回滚、invalid 不入上下文、图超时 partial、检索 trace 与 UI 详情一致，以及 embedding 签名变化后的重建边界。
