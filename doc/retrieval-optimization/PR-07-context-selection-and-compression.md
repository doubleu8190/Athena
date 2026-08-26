# PR 07：上下文选择与安全压缩

## 1. 文档状态

- 状态：待评审
- 前置依赖：PR 01～PR 06
- 阻塞范围：PR 08 最终切流
- 运行时行为变化：Context Assembly 按开关启用

## 2. 背景

Reranker 解决相关性排序，但直接截取 Top N 仍会产生重复重叠、来源单一、Child 缺少 Parent Header、长 PDF 浪费 Token，以及生成式压缩破坏数字/代码等问题。更重要的是，召回文档可能包含看似指令的文本，这些内容必须被视为不可信数据，而不是用户请求或系统指令。

本 PR 将 Candidate Ranking 与最终 Context Packaging 分离。

## 3. 目标

1. 在明确 Token Budget 下选择多样、非重复的上下文。
2. 仅在选中 Child 后扩展 Parent/Neighbor。
3. 完整保留数字、代码、表格和 Locator。
4. 只压缩符合条件的长 Prose。
5. 将所有召回内容标记为不可信 Evidence。
6. 准确记录真正注入的 Item ID。

## 4. 非目标

- 不修改候选召回和 Reranker。
- 不摘要 Atomic Memory、Code 或 Spreadsheet Data。
- 不执行召回内容中的任何指令。
- 不绕过 Agent Tool 授权。

## 5. Context 模型

```python
@dataclass
class ContextItem:
    item_id: str
    source_type: str
    content: str
    locator: dict[str, Any]
    token_count: int
    relevance_score: float
    parent_id: str | None
    content_hash: str
    compression_policy: str

@dataclass
class ContextPackage:
    items: list[ContextItem]
    total_tokens: int
    omitted_item_ids: list[str]
    compressed_item_ids: list[str]
    citations: dict[str, dict[str, Any]]
```

Package 与数据源无关；Memory System Context 和 File Tool Response 仍在各自边界格式化。

## 6. 选择流程

```text
Reranked Top 10
  -> Stable ID / Exact Hash 去重
  -> Overlap / Near-duplicate 合并
  -> Source Diversity 选择
  -> Parent / Adjacent 扩展
  -> 可选 Prose Compression
  -> Token Budget Packing
  -> Citation 校验
```

任何扩展都不能越过 Trusted Source Scope。

## 7. 去重与多样性

去重分三层：Stable ID、规范化 Content Hash、Token Shingle 或现有 Embedding 的 Near-duplicate。对同 Parent 的 Overlap Chunk，只保留最高 Rank，并仅合并邻居的非重复部分；所有贡献 Locator 都保留。

不同事实即使共享模板或表头，也不能仅凭文本相似度去重。

多样性使用有界 MMR：

```text
selection_score = relevance - lambda * max_similarity_to_selected
```

只在相关度接近的候选间应用。强 Exact 和 Complex Query 各 Subquestion 的必要证据不能只为多样性被移除。

## 8. Parent/Adjacent 扩展

| 数据源 | 扩展策略 |
| --- | --- |
| Memory Fact | 不扩展。 |
| Memory Summary | 默认不扩展。 |
| PDF Child | 同 Page/Section 增加 Heading 和有限上下文。 |
| Code Symbol | 增加 Signature/Import 或 Symbol 边界内相邻行。 |
| Excel Row Window | 必须保留 Header，可增加一个相邻 Window。 |
| Generic Text | 边界不完整时增加一个邻接 Chunk。 |

扩展内容继承主候选 Citation，同时记录自己的 Source ID。

## 9. 压缩策略

| 内容 | 策略 | 原因 |
| --- | --- | --- |
| Atomic Memory | 不压缩 | 已短且结构化。 |
| Conversation Summary | 不压缩 | 已是生成摘要。 |
| Code | 不压缩 | 可能破坏 Identifier 和语义。 |
| Excel/Table | 不压缩 | 必须保留精确值和 Header。 |
| Error Log | 只抽取相关原始行 | 保留 Literal Error。 |
| 长 PDF/Prose | 抽取式或 Query-focused | 去除无关段落。 |

首版优先使用确定性 Sentence Extraction。只有无法满足 Budget 时才考虑 LLM Compression。

## 10. Compressor Port

```python
class RetrievalContextCompressor(Protocol):
    async def compress(
        self,
        query: str,
        item: ContextItem,
        max_tokens: int,
    ) -> CompressedContextItem: ...
```

输出必须包含 Source Span 或 Sentence Index。无法追溯时拒绝压缩结果，改用受限长度的原文。

LLM Compression 必须：

- 优先抽取式输出。
- 保留 Name、Number、Date、Error Code 和 Negation。
- 接受前校验关键 Literal。
- 超时、Span 非法或 Literal 丢失时回退。
- 与现有 Conversation History `ContextCompressor` 使用不同名称和职责。

## 11. 指令与 Prompt Injection 边界

Memory 和 Document 中的召回内容一律是不可信数据。即使包含“忽略之前指令”、工具调用要求或其他命令，也不能改变 Agent Policy、Tool Permission 或任务 Scope。

格式必须明确分隔：

```text
[RETRIEVED_EVIDENCE id="..." source="pdf" page="12"]
...source content...
[/RETRIEVED_EVIDENCE]
```

可信 Prompt 明确说明 Evidence 只用于回答用户问题，内部指令不具备控制权。具体约束：

- 不把召回正文无分隔拼接进可信 System Prompt。
- 不从召回正文解析 Tool Call。
- Compression Prompt 不能把 Evidence 重新解释为 Authority。
- 当用户询问文档中的指令时，可以引用，但必须标注为“文档内容”。

## 12. Token Budget

预先划分：Answer/Tool Reserve、Memory Evidence、Attachment Evidence、Expansion。子预算为软限制，总预算为硬限制；未使用的 Memory Budget 可转给 Attachment，但不能改变授权和相关性顺序。

建议初始值：

```python
retrieval_context_k: int = 5
retrieval_context_max_tokens: int = 4000
retrieval_context_memory_max_tokens: int = 1200
retrieval_context_expansion_max_tokens: int = 1200
retrieval_context_mmr_lambda: float = 0.75
```

现有 `memory_max_tokens` 在配置统一前继续作为兼容上限。

## 13. Citation

- Memory：Memory ID + Type。
- PDF：Filename + Page + 可选 Table/Section。
- Excel：Filename + Sheet + Row Range/Cell。
- Code：Path + Start/End Line + Symbol。
- Text：Filename + Paragraph/Chunk Locator。

Compression 不能删除、修改或伪造 Locator。Citation 在授权检查后、内容格式化前分配。

## 14. 文件改动

```text
athena/core/retrieval/context.py
athena/core/retrieval/dedup.py
athena/core/retrieval/compression.py
athena/core/memory/retrieval.py
athena/core/files/runtime.py
athena/core/agent/workflow.py
prompt/retrieved_evidence.md
prompt/context_compression.md
athena/config/settings.py
tests/test_context_selection.py
tests/test_context_security.py
```

## 15. 测试与验收

测试覆盖：重复/Overlap 只占一次 Budget；强 Exact 不被 MMR 删除；Complex Query 尽量覆盖每个 Subquestion；PDF 扩展不越 Parent；Code 扩展不越 Path/Symbol；Excel 始终保留 Header；Code/Table/Atomic Memory 不调用 LLM Compressor；压缩保留 Critical Literal 和 Span；恶意文档指令保持 Evidence 身份，不能生成 Tool Call 或修改 System Policy；Access ID 与实际注入 ID 完全一致。

验收标准：Precision@5 和 Citation Accuracy 不低于 PR 06；长文档 Token 减少且 nDCG/忠实度不下降；数字、代码、错误码 Fixture 字节级保留；所有 Context Item 都有合法 Locator；Compression 失败可降级到受限原文。

## 16. 发布与回滚

先启用确定性 Selection，再单独启用 LLM Compression。Shadow 比较 Selected ID、Token 和 Citation。关闭 Compression 不影响 Dedup/Selection；关闭 Context Assembly 开关可回退 PR 06 Top K。

## 17. 风险

| 风险 | 缓解措施 |
| --- | --- |
| 多样性删除同 Section 必要证据 | 只在相近分数间应用，并保证 Subquestion Coverage。 |
| 压缩改变含义 | 抽取优先、Literal 校验、失败回退。 |
| Parent 扩展超预算 | 预留 Expansion Budget，按 Source Boundary 截取。 |
| 文档指令影响 Agent | 不可信数据分隔和安全回归测试。 |

