# PR 02：修复召回正确性与分数语义

## 1. 文档状态

- 状态：待评审
- 前置依赖：PR 01
- 阻塞范围：PR 03、PR 06
- 运行时行为变化：有，通过 Feature Flag 控制

## 2. 背景

当前长期记忆链路会用改写 Query 替换原始向量 Query，用 RRF 分数覆盖原始相似度，再叠加生命周期乘数并用 RRF 派生阈值过滤。这导致：

1. 改写漂移会丢失原始实体。
2. RRF 只有相对排名，不能表示绝对相关性。
3. 纯关键词结果通常无法越过现有阈值。
4. 候选刚被检索就增加访问次数，形成错误的热度反馈。

文件检索还存在 FTS 原始表达式未安全构造、未显式按 BM25 排序，以及融合后丢失向量原始分数的问题。

## 3. 目标

1. 为每类分数定义唯一含义。
2. 原始 Query 必须始终参与语义召回。
3. 强 Exact/Keyword 证据可以独立返回。
4. 低相似度的向量 Top 1 不得仅凭排名通过。
5. 只为真正注入上下文的记忆记录访问。
6. 拆分候选、重排和上下文 Top K。

## 4. 非目标

- 不引入 Query Classifier 或 Multi-Query。
- 不增加学习型 Reranker。
- 不重做文档切块。
- 不改变外部文件工具参数。

## 5. 结果模型

替换当前被重载的 `score` 语义：

```python
@dataclass
class SearchResult:
    content: str
    item_id: str
    source: str
    metadata: dict[str, Any]
    native_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None
    exact_match: bool = False
    rank_sources: dict[str, int] = field(default_factory=dict)
```

迁移期保留兼容属性：

```python
@property
def score(self) -> float:
    return self.rerank_score or self.fused_score or self.native_score or 0.0
```

所有调用方迁移完成后再单独移除该属性。

## 6. Query 执行流程

本 PR 固定执行：

```text
原始 Query -> Keyword
原始 Query -> Vector
改写 Query -> Vector，仅当内容确实不同
```

改写失败只取消附加 Query，不能替换或污染原始 Query。独立检索尽量并发执行，并遵循统一超时和取消机制。

## 7. 通道准入

在 RRF 之前使用各通道原生证据过滤：

- Vector：`native_score >= memory_vector_min_score`。
- Exact：除非被显式 metadata 排除，否则直接进入候选。
- Keyword：通过 Exact 或有序 BM25 排名进入，不与向量相似度直接比较。
- 某一路失败：记录降级原因，其余路线继续。

建议初始配置，最终值由 PR 01 数据校准：

```python
retrieval_candidate_k: int = 30
retrieval_rerank_k: int = 10
retrieval_context_k: int = 5
memory_vector_min_score: float = 0.70
retrieval_pipeline_mode: Literal["legacy", "corrected"] = "legacy"
```

现有 `retrieval_top_k` 在一个兼容周期内作为默认回退，之后废弃。

## 8. 融合语义

由于 BM25 和向量分数未校准到同一分布，继续使用加权 RRF 排序：

```text
fused_score(item) = sum(route_weight / (rrf_k + route_rank))
```

约束：

- RRF 只排序已经准入的候选，不承担相关性阈值判断。
- 用稳定 Item ID 去重。
- 同一结果在原始和改写 Query 中的证据分别保留。
- Exact 使用独立标记，不伪造向量分数。
- 在 PR 06 之前，排序为 Exact Tier、RRF、生命周期 Tie-break、稳定 ID。

生命周期信号不能把低相关度 Tier 提升到高相关度 Tier 之前。

## 9. 访问与生命周期

从 `MemoryManager.search()` 和 `keyword_search()` 移除 `_record_access()`，新增：

```python
def record_selected_access(self, memory_ids: Iterable[str]) -> None:
    ...
```

`MemoryRetrievalService` 仅对真正写入 `[相关记忆]` 的 ID 调用；被 Token Budget 跳过的候选不计访问。同一请求内先去重，双路命中最多增加一次。

生命周期策略：

- pinned 仅作为相同相关度下的稳定信号。
- 频率和最近访问只做次级排序。
- pinned 不应用创建时间衰减。
- 生命周期分数不参与候选准入。

## 10. 文件 FTS 修复

`SqliteFileRepository.search_chunks()` 不再把原始 Query 直接传给 `MATCH`：

- 转义引号和 FTS 操作符。
- 保留错误码、下划线、点号和版本号。
- 查询 `bm25(file_chunk_fts) AS rank` 并 `ORDER BY rank`。
- 返回原始 BM25 Rank。
- 仅当无法产生 Token 或 FTS 执行失败时使用受控 `contains()` 回退。
- 所有值使用绑定参数，禁止字符串拼接 SQL。

## 11. 文件改动

```text
athena/core/memory/retrieval.py
athena/core/memory/memory.py
athena/core/memory/ports.py
athena/core/files/runtime.py
athena/infrastructure/sqlite/file_repository.py
athena/config/settings.py
tests/test_memory.py
tests/test_file_intelligence.py
tests/test_retrieval_scoring.py
```

共享结果模型放在 core，不得导入具体基础设施类型。

## 12. 兼容性

- `get_relevant_memories()` 签名不变。
- `search_file()` 工具响应保留现有字段。
- Legacy 与 Corrected 模式共用现有数据和索引。
- 旧配置在一个版本周期内继续可读。

## 13. 测试方案

- Rewrite 漂移时仍执行原始向量 Query。
- 空 Rewrite 不生成垃圾查询。
- 原始相似度低于阈值的 Vector Top 1 被拒绝。
- Keyword-only 错误码可以返回。
- 多路相同结果只出现一次，但保留所有来源 Rank。
- 候选检索不增加访问次数。
- Token Budget 实际选中的记忆只增加一次访问。
- pinned 旧记忆不受创建时间衰减。
- FTS 特殊字符不产生未处理 SQL 错误。
- 文件关键词结果严格按 BM25 排序。
- 单路失败时其余结果正常返回。

## 14. 验收标准

- Legacy 模式现有测试全部通过。
- Corrected 模式通过全部新分数语义测试。
- Exact/API/错误码 Hit@3 不低于 PR 01 基线。
- 空结果误召回率不升高。
- 上下文选择前不持久化任何访问次数。
- 简单查询 P95 延迟增幅不超过 20%。

## 15. 发布与回滚

首次发布保持 `retrieval_pipeline_mode="legacy"`，先离线比较，再在受限环境启用 Corrected。回滚只改配置，不需要数据迁移。

## 16. 风险

| 风险 | 缓解措施 |
| --- | --- |
| 向量阈值误伤召回 | 按切片使用 Recall@30 校准。 |
| Exact Tier 被模糊短词滥用 | 要求完整字段或 Token 边界匹配。 |
| 附加向量 Query 增加延迟 | 去除重复 Rewrite，并发执行。 |
| 移除候选访问导致排序变化 | 视为预期修复，并通过 Shadow Trace 比较。 |

