# PR 03：Query Analyzer 与 RetrievalPlan

## 1. 文档状态

- 状态：待评审
- 前置依赖：PR 01、PR 02
- 阻塞范围：PR 04～PR 07
- 运行时行为变化：消费者启用前仅记录分析结果

## 2. 背景

当前长期记忆和附件检索各自采用固定策略。FAQ、错误码、API/Class、自然语言、Complex 不能设计成互斥单标签，因为一条 Query 可能同时包含自然语言、异常码、类名和多跳排障意图。

本 PR 建立多维 Query 分析协议，只描述应该执行的检索工作，不直接依赖 SQLite、Chroma 或文件 Adapter。

## 3. 目标

1. 把用户 Query 和受信任 Scope 转换成可校验的 `RetrievalPlan`。
2. 高置信度词法信号不调用 LLM。
3. 最多一次 LLM 请求完成分类、改写和 metadata 提取。
4. 支持多标签、多路线和软硬过滤。
5. LLM 超时或输出非法时确定性降级。

## 4. 非目标

- Analyzer 不直接执行检索。
- 不接受 LLM 生成的 Session/Attachment 授权范围。
- 本 PR 不实现 Multi-Query Fan-out。
- 推测出的 metadata 不直接做硬过滤。

## 5. 领域模型

新增 `athena/core/retrieval/models.py`：

```python
class QueryIntent(str, Enum):
    LOOKUP = "lookup"
    EXPLAIN = "explain"
    TROUBLESHOOT = "troubleshoot"
    COMPARE = "compare"
    AGGREGATE = "aggregate"
    SUMMARIZE = "summarize"

class RetrievalRoute(str, Enum):
    EXACT = "exact"
    KEYWORD = "keyword"
    VECTOR = "vector"
    SYMBOL = "symbol"
    GRAPH = "graph"
    TABLE = "table"

@dataclass(frozen=True)
class RetrievalPlan:
    original_query: str
    intent: QueryIntent
    complexity: Literal["simple", "complex"]
    source_scopes: tuple[str, ...]
    routes: tuple[RetrievalRoute, ...]
    exact_terms: tuple[str, ...]
    semantic_queries: tuple[str, ...]
    hard_filters: Mapping[str, Scalar]
    soft_filters: Mapping[str, Scalar]
    confidence: float
    analyzer_mode: str
    reasons: tuple[str, ...]
```

`source_scopes` 只能是 memory、pdf、excel、code 等可信类别。真实 Attachment ID 必须来自 Workflow/Tool Context，不能来自模型输出。

## 6. 分析流程

```text
Query + Trusted Scope
        |
只做不改变 Literal 的规范化
        |
确定性规则检测
        |
高置信度？ -- 是 --> 校验并返回
        |
        否
        v
单次结构化 LLM 分析
        |
合并规则证据和 LLM Hint
        |
校验；非法则返回 Hybrid Fallback
```

原始 Query 永远是第一条 Semantic Query，任何阶段不得删除。

## 7. 确定性规则

在 `rules.py` 中实现，并提供中英文 Fixture：

| 信号 | 示例 | 推荐路线 |
| --- | --- | --- |
| 错误/HTTP Code | `E11000`、`ORA-00942`、`HTTP 502` | Exact、Keyword、Vector |
| Symbol | `UserService`、`parse_config()` | Symbol、Exact、Keyword |
| Qualified Name | `foo.bar.Client.send` | Exact、Symbol |
| 文件路径 | `src/auth/token.py` | Exact、Keyword |
| 版本号 | `v2.4.1`、`Python 3.12` | Exact、Keyword、Vector |
| Locator | `第12页`、`Sheet2`、`B42` | Exact + Hard Filter |
| 聚合 | `总和`、`平均`、`top 10` | Table |
| 记忆引用 | `之前`、`上次决定`、`我的偏好` | Memory Scope Boost |
| 多子句 | `比较...并解释...` | Complex |

Regex 必须预编译并限制输入长度。识别到的用户文本只能作为数据，不能直接作为 SQL、FTS 或代码执行。

## 8. LLM 输出协议

新增 `prompt/query_analysis.md`，仅允许 JSON：

```json
{
  "intent": "troubleshoot",
  "complexity": "complex",
  "source_hints": ["code"],
  "routes": ["symbol", "keyword", "vector"],
  "exact_terms": ["NullPointerException", "UserService"],
  "rewritten_query": "UserService NullPointerException root cause",
  "soft_filters": {"language": "java"},
  "confidence": 0.84,
  "reasons": ["检测到异常名和类名"]
}
```

Pydantic 校验后的合并规则：

1. 受信任请求 Scope 覆盖 `source_hints`。
2. 规则识别的 Exact Terms 不允许被 LLM 删除。
3. 丢弃不支持的 Route 和 Filter Key。
4. 用户未明确表达的 Filter 默认降为 Soft Filter。
5. 空、重复或超长 Rewrite 直接丢弃。
6. 非法 JSON 返回确定性 Hybrid Plan。

## 9. Router 协议

```python
class Retriever(Protocol):
    source: str

    async def retrieve(
        self,
        plan: RetrievalPlan,
        context: RetrievalContext,
    ) -> list[SearchResult]: ...
```

`RetrievalContext` 保存可信 Session、Attachment Scope、Budget、Request ID 和取消信号，不保存基础设施 Client。

Router 负责：

- 只选择当前 Context 授权的 Retriever。
- 并发执行独立路线。
- 遵守全局 Deadline。
- 单路失败时保留其他结果。
- 返回候选和 Trace，不负责格式化 LLM Context。

## 10. 配置

```python
query_analyzer_enabled: bool = False
query_analyzer_llm_fallback: bool = True
query_analyzer_timeout_ms: int = 3000
query_analyzer_max_query_chars: int = 4000
query_analyzer_min_rule_confidence: float = 0.90
```

Analyzer 复用 `athena/main.py` 中已有 Secondary LLM，由 Composition Root 注入。

## 11. 文件改动

```text
athena/core/retrieval/__init__.py
athena/core/retrieval/models.py
athena/core/retrieval/rules.py
athena/core/retrieval/analyzer.py
athena/core/retrieval/router.py
athena/core/retrieval/ports.py
prompt/query_analysis.md
athena/config/settings.py
athena/main.py
tests/test_query_analyzer.py
tests/test_retrieval_router.py
```

## 12. 失败策略

| 故障 | 行为 |
| --- | --- |
| 规则解析异常 | 返回默认 Hybrid Plan，并记录 Error Code。 |
| LLM 超时 | 返回规则增强后的 Fallback。 |
| JSON 非法 | 只记录 Schema Error，不记录完整响应。 |
| Filter 不支持 | 丢弃并写入 Validation Reason。 |
| 单个 Retriever 失败 | 返回成功路线候选。 |
| 全局超时 | 取消未完成 Task，返回已完成候选。 |

## 13. 测试方案

- 参数化覆盖错误码、路径、版本、页码、Sheet 和聚合表达。
- 混合 Query 产生多个 Route，而不是单标签。
- 所有路径都保留原始 Query。
- LLM 不能增加未授权 Attachment。
- 用户明确页码/Sheet 成为 Hard Filter。
- 推测的语言只能成为 Soft Filter。
- 非法 JSON、超时走确定性 Fallback。
- Router 确实并发执行独立 Retriever。
- 单路失败与取消不破坏部分结果。
- 架构测试禁止 core 导入 infrastructure。

## 14. 验收标准

- 高置信度 Exact Query 不新增 LLM 调用。
- PR 01 全部 Query 都能生成合法 Plan。
- 人工标注 Exact Signal 保留率至少 95%。
- Analyzer 故障不阻断基础检索。
- 模型输出无法扩大受信任 Scope。
- Trace 可见 Analyzer Mode、耗时和降级原因。

## 15. 发布与回滚

先以 Trace-only 方式运行 Analyzer，只记录 Plan，继续执行 PR 02 固定 Hybrid。PR 04、PR 05 分别启用实际消费。关闭 `query_analyzer_enabled` 即恢复固定链路。

## 16. 风险

| 风险 | 缓解措施 |
| --- | --- |
| 一条 Query 属于多个类型 | 使用多维 Plan，不做单标签互斥。 |
| LLM 推测错误 Filter | 默认作为 Soft Filter。 |
| Regex 把普通文本误判为代码 | 多信号确认或降低置信度。 |
| 简单 Query 延迟增加 | 高置信度规则直接短路。 |

