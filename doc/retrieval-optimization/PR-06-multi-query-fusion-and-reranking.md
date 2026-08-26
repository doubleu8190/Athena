# PR 06：Multi-Query、融合与 Reranker

## 1. 文档状态

- 状态：待评审
- 前置依赖：PR 01～PR 05
- 阻塞范围：PR 07、PR 08 上线门槛
- 运行时行为变化：有，Feature Flag 控制

## 2. 背景

Multi-Query 能提高歧义和多跳问题的 Recall，但对所有 Query 启用会放大弱命中并增加延迟。RRF 可以融合异构排名，却不能理解 Query 与正文是否真正相关，因此高召回候选之后还需要独立 Reranker。

本 PR 增加有预算约束的 Query 分解、保留原始证据的融合，以及可替换 Reranker。

## 3. 目标

1. 只为复杂或低置信度 Query 开启 Multi-Query。
2. 区分同义改写和正交子问题。
3. 全局限制 Query、Route、Candidate 和 Deadline。
4. 融合后保留所有 Native Evidence。
5. 对少量候选执行 Query-aware Rerank。
6. 支持本地、Provider 和 No-op Reranker。
7. 在 Rerank 后按数据源校准 Abstention。

## 4. 非目标

- Context Compression 属于 PR 07。
- Reranker 不作为基础链路的强依赖。
- Reranker 不能覆盖授权和 Hard Filter。
- Multi-Query 不用于掩盖索引缺失或损坏。

## 5. 启用条件

满足任一条件时允许 Multi-Query：

- `complexity == complex`。
- Analyzer Confidence 低于阈值。
- Query 包含两个以上可独立回答的子句。
- 首轮没有候选通过 Native Admission，且 Deadline 允许一次补救。

以下情况禁止启用：

- Error Code 或 Symbol 已有强 Exact Hit。
- 明确 Page/Sheet/Cell/Path/Line 查询。
- 已生成确定性 `TableQueryPlan` 的简单聚合。
- 整体 Deadline 不足。

## 6. Query Variant 模型

```python
class QueryVariantKind(str, Enum):
    ORIGINAL = "original"
    PARAPHRASE = "paraphrase"
    SUBQUESTION = "subquestion"

@dataclass(frozen=True)
class QueryVariant:
    text: str
    kind: QueryVariantKind
    weight: float
    target_routes: tuple[RetrievalRoute, ...]
```

约束：

- Original 永远存在，Weight 为 1.0。
- 最多生成 3 条，总数不超过 4。
- 必要 Exact Term 必须保留。
- 规范化后重复 Variant 去重。
- Subquestion 必须覆盖原问题的不同部分。
- Variant 无权新增 Attachment ID 或 Hard Filter。

## 7. Prompt

新增 `prompt/multi_query.md`，返回结构化 JSON：

```json
{
  "variants": [
    {
      "text": "UserService NullPointerException definition and call sites",
      "kind": "subquestion",
      "target_routes": ["symbol", "graph"]
    }
  ]
}
```

Prompt 要求 Query 正交、只输出 JSON。非法响应直接退回 Original-only。

## 8. 全局预算与并发

```python
@dataclass(frozen=True)
class RetrievalBudget:
    max_variants: int = 4
    max_route_calls: int = 10
    max_candidates_per_call: int = 30
    max_unique_candidates: int = 100
    deadline_ms: int = 8000
```

Original 和 Exact Route 优先获得预算。Router 在 Semaphore 下并发执行；超时后忽略并取消未完成调用。

## 9. Evidence-preserving Fusion

```python
@dataclass
class CandidateEvidence:
    query_index: int
    query_kind: str
    route: str
    rank: int
    native_score: float | None
    exact_match: bool
```

融合公式：

```text
fused_score = sum(query_weight * route_weight / (rrf_k + rank))
```

规则：

- Stable Item ID 去重。
- Original Query Weight 最高。
- Exact Evidence 独立保留。
- 同一结果跨 Variant 获得有限加成，每 Route 设贡献上限，防止同义 Query 刷分。
- Native Score 不丢失。
- Hard Filter 排除的候选不能被融合恢复。

融合后取 20～30 个 Unique Candidate 进入 Rerank，具体值由 Recall/Latency 曲线决定。

## 10. Reranker Port

```python
class Reranker(Protocol):
    async def rerank(
        self,
        query: str,
        candidates: Sequence[RerankDocument],
        limit: int,
    ) -> list[RerankScore]: ...
```

`RerankDocument` 包含受长度限制的正文、Source Type、Title/Topic/Path/Signature、Locator 和 Exact Evidence。

实现：

1. `NoopReranker`：回退和单元测试。
2. infrastructure 中的 `CrossEncoderReranker`：本地配置模型。
3. 显式配置时才启用 Provider-backed Reranker。

首轮评估至少包括适合中英文的多语言 Cross-Encoder，例如 `bge-reranker-v2-m3`；Code Slice 还需评估 Code-aware 模型。模型依赖和权重应为 Optional，并由部署预置，生产运行时不得静默下载。

## 11. 最终排序与 Abstention

排序优先级：

1. Authorization 和 Hard Filter。
2. Literal Lookup Intent 下的强 Exact Evidence。
3. Reranker Relevance。
4. Fused Score，用于 Tie 和 Fallback。
5. 有界 Source Quality Signal。
6. Stable ID。

解释型问题中，Exact 只是强特征，不应无条件超过真正回答问题的 Passage。

Abstention 按 Memory、General Document、Code、OCR、Table 分别校准，禁止对不可比分数使用单一全局阈值。无候选通过时返回空结果和 `abstention_reason`。

## 12. Cache

- Query Analysis/Variant：按 Provider、Model、Prompt Version 和 Query Hash 短期缓存。
- Rerank Score：按 Query Hash、Content Hash、Model Version 和截断策略缓存。
- 含候选 ID 的缓存不能跨授权 Scope 复用。
- 错误响应不缓存或只做极短缓存。

## 13. 文件改动与配置

```text
athena/core/retrieval/multi_query.py
athena/core/retrieval/fusion.py
athena/core/retrieval/reranker.py
athena/core/retrieval/budget.py
athena/infrastructure/rerank/cross_encoder.py
prompt/multi_query.md
athena/config/settings.py
athena/main.py
pyproject.toml
tests/test_multi_query.py
tests/test_retrieval_fusion.py
tests/test_reranker.py
```

```python
retrieval_multi_query_enabled: bool = False
retrieval_multi_query_max: int = 4
retrieval_multi_query_confidence_threshold: float = 0.70
retrieval_max_route_calls: int = 10
retrieval_max_unique_candidates: int = 100
retrieval_reranker_backend: str = "none"
retrieval_reranker_model: str = ""
retrieval_rerank_k: int = 10
retrieval_deadline_ms: int = 8000
```

## 14. 失败策略

| 故障 | 行为 |
| --- | --- |
| Variant 生成失败 | 仅使用 Original。 |
| 单路失败 | 融合已完成 Evidence。 |
| Budget 耗尽 | 停止低优先级调用并记录 Truncation。 |
| Reranker 不可用/超时 | 使用 Fused Ordering。 |
| Reranker 返回未知 ID | 丢弃非法行，保留原 Candidate Mapping。 |

## 15. 测试与验收

测试覆盖：Simple Exact 只产生 Original；Complex 产生有界正交 Variant；Exact Term 不丢失；全局 Route/Candidate Budget 生效；重复结果去重并保留 Evidence；单 Variant 无法刷分；错误码 Rerank 后不丢；自然语言 Answer Passage 可超过只命中标题的结果；Reranker 超时确定性回退；Expected-empty 正确 Abstain。

验收门槛：总体 nDCG@10 相对 PR 01 提升至少 10%；Complex Recall@30 提升且 Exact Hit@3 不下降；Exact/API/Error Code Hit@3 至少 95%；空结果误召回率不超过 5%；简单规则 Query 不调用 Multi-Query。

## 16. 发布与回滚

Multi-Query 和 Reranker 使用独立开关，先 Shadow 记录候选和 Rank 差异但不返回。关闭任一开关即可回退到 PR 05 Retriever 和 PR 02 Fusion。

## 17. 风险

| 风险 | 缓解措施 |
| --- | --- |
| Recall 提升但延迟过大 | Eligibility、全局预算、并发和 Cache。 |
| Reranker 破坏 Exact Lookup | 保留 Exact Evidence，分切片验收。 |
| 本地模型资源过大 | Optional Backend、启动预检、No-op Fallback。 |
| Query Variant 漂移 | 保留 Original，并验证 Exact Entity。 |
