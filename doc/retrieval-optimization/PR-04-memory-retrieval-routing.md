# PR 04：长期记忆分型召回与路由

## 1. 文档状态

- 状态：待评审
- 前置依赖：PR 01～PR 03
- 阻塞范围：PR 06、PR 07
- 运行时行为变化：有，Memory Route 按开关启用

## 2. 背景

Athena 当前把两种语义结构不同的记忆放进同一检索空间：

- 原子事实：`key: value`。
- 对话摘要：`topic: content`。

两者使用相同 Rewrite、Vector、Keyword、RRF、生命周期权重和结果预算。同时，Workflow 会为每条用户消息尝试长期记忆召回，即使用户只是在查询附件代码或 Excel。

本 PR 将长期记忆变成受 `RetrievalPlan` 控制的数据源，并分别处理 Fact 与 Summary，同时兼容已有数据。

## 3. 目标

1. Query 与长期记忆无关时跳过或降低 Memory Route。
2. Fact 和 Conversation Summary 分别召回。
3. 支持 Fact Key 精确检索。
4. 不要求破坏性数据库迁移。
5. 生命周期信号始终低于相关性证据。
6. 只为实际注入的记忆记录访问。

## 4. 非目标

- 不合并 Memory Store 和 File Store。
- 不让文件工具跨 Session 读取长期记忆。
- 本 PR 不增加学习型 Reranker。
- 不在读取时原地改写旧记忆。

## 5. Memory Route 判定

满足以下任一条件时执行 Memory Retrieval：

- Analyzer 识别到“之前、上次、我的偏好”等明确记忆引用。
- Intent 涉及偏好、Profile、历史决定、历史解决方案或未决事项。
- 调用方明确要求 Memory Scope。
- Analyzer 置信度较低，兼容策略要求补充 Memory Route。

当可信数据源全部是已就绪附件，且 Query 只询问其正文、代码、表格或摘要时，跳过自动长期记忆注入。该策略不影响 Session History。

## 6. 分型策略

| 类型 | 主路线 | 辅助路线 | 默认候选数 |
| --- | --- | --- | ---: |
| `fact` | Exact Key + Vector Value | Keyword Content | 20 |
| `conversation_summary` | Vector Content | Keyword Topic/Content | 20 |
| 未知旧类型 | Corrected Hybrid | 无 | 20 |

未知类型必须保留兼容回退，不能因历史数据缺少 metadata 而丢失。

## 7. 向后兼容的结构化 Metadata

仓库当前明确没有通用启动迁移 Hook，因此新写入使用增量 Metadata，而不是修改已有表列。

```python
# Atomic fact
content = "preferred_language: Rust"
metadata = {
    "type": "fact",
    "fact_key": "preferred_language",
    "fact_value": "Rust",
}

# Conversation summary
content = "database migration: decided to move from MySQL to PostgreSQL"
metadata = {
    "type": "conversation_summary",
    "topic": "database migration",
}
```

Metadata 同时写入 SQLite `metadata_json` 和 Chroma Metadata。已有 `type/category/confidence/source` 顶层字段继续使用。

旧数据仅在类型明确时，允许从第一个冒号前派生 `fact_key` 或 `topic`；读取过程不得写回存储。

## 8. Exact Fact Search

扩展 `MemoryRepository`：

```python
async def exact_search(
    self,
    terms: Sequence[str],
    limit: int,
    where: dict[str, Any] | None,
) -> list[dict[str, Any]]: ...
```

SQLite 查询顺序：

1. 仅为比较规范化 snake_case 和大小写。
2. 新数据匹配 `json_extract(metadata_json, '$.fact_key')`。
3. 已标记为 Fact 的旧数据使用安全 `content LIKE :key_prefix` 回退。
4. 用户明确引用完整短语时匹配规范化完整 Content。

所有用户值必须使用绑定参数。Exact 结果返回 `exact_match=True` 和 `fact_key` 等原因，不伪造向量分数。

## 9. Fact 与 Summary Query

Fact Retriever 使用：

- `RetrievalPlan.exact_terms` 查询 Key 和 Literal Value。
- 原始 Query 查询 Semantic Value。
- Rewrite 仅作为附加 Vector Route。
- 用户明确 Type/Category 时使用 Hard Filter。
- 推测 Metadata 只做 Soft Ordering。

Summary Retriever 以 Semantic 为主，因为 Topic 与叙述不一定和用户原词相同。Summary 可以获得更大的单条 Token Budget，但受全局 Candidate Cap 限制。

## 10. 上下文选择协议

在格式化字符串前先返回结构化选择结果：

```python
@dataclass
class MemoryContextSelection:
    results: list[SearchResult]
    selected_ids: list[str]
    token_count: int
    truncated: bool
```

`get_relevant_memories() -> str` 暂时保留，通过该结构生成现有 `[相关记忆]` 文本，然后调用 `record_selected_access(selected_ids)`。

## 11. Confidence 与生命周期

- Fact Extraction Confidence 默认作为软特征。
- pinned 只在相近相关性候选中优先。
- Frequency 和 Last Access 仅做 Tie-break。
- 创建时间不能拒绝语义上强相关的记忆。
- 新旧事实冲突不能按热度决定真伪；必须保留时间和 Confidence 供后续处理。

## 12. Workflow 集成

当前流程先召回 Memory，再处理附件。调整为：

```text
规范化请求
  -> 加载可信附件 Metadata
  -> 分析 Query 与 Scope
  -> 仅在 Plan 需要时召回 Memory
  -> 继续原有附件 Ready/Waiting 流程
```

Analyzer 只接收附件类型和状态，不读取附件正文。Pending Attachment 行为保持不变。

## 13. 文件改动

```text
athena/core/memory/retrieval.py
athena/core/memory/memory.py
athena/core/memory/ports.py
athena/core/memory/summarizer.py
athena/core/agent/workflow.py
athena/infrastructure/sqlite/memory_repository.py
prompt/fact_extraction.md
prompt/conversation_summary.md
athena/config/settings.py
tests/test_memory.py
tests/test_memory_routing.py
```

## 14. 配置

```python
memory_routing_enabled: bool = False
memory_fact_candidate_k: int = 20
memory_summary_candidate_k: int = 20
memory_exact_match_enabled: bool = True
memory_unknown_type_fallback: bool = True
```

PR 02 的全局 Candidate 和 Context Budget 仍是硬上限。

## 15. 兼容与维护

- 旧 SQLite Row 和 Chroma Document 继续可读。
- 新 Metadata 仅增量写入。
- 启动时不执行破坏性 Rewrite。
- 另行提供显式维护命令检查和可选重建旧 Metadata，必须支持 Dry-run、批处理和续跑。
- 关闭 Memory Routing 后恢复 PR 02 的 Corrected Hybrid。

## 16. 测试方案

- 新 Fact 包含 `fact_key/fact_value`。
- 新 Summary 包含 `topic`。
- 旧 Fact 只派生、不写回 Key。
- Exact Key Query 在 Top 1 命中。
- Summary Query 保持 Semantic Route。
- 附件专属 Query 跳过自动 Memory Retrieval。
- 明确历史决定 Query 即使带附件也会召回 Memory。
- 只有 Selected ID 增加访问。
- 保持现有跨 Session Memory 产品策略。
- 不支持的 Metadata Filter 安全失败。

## 17. 验收标准

- Fact Hit@3、Summary nDCG@10 不低于 PR 01，且误召回率下降或持平。
- 附件专属用例不注入无关 Memory。
- 现有记忆无需强制迁移。
- Exact Query 使用绑定参数并通过特殊字符测试。
- Memory Route 故障不阻断 Agent Workflow。

## 18. 发布与回滚

先在 Planned/Shadow 模式启用，对比 Selected ID、空结果和 Prompt Token。关闭 `memory_routing_enabled` 即回退；增量 Metadata 无需删除。

## 19. 风险

| 风险 | 缓解措施 |
| --- | --- |
| Analyzer 跳过隐含有用记忆 | 低置信度保留兼容回退。 |
| 旧 Key 解析有歧义 | 只解析明确标记为 Fact 的记录。 |
| SQLite JSON 查询在大库变慢 | 限制候选；需要索引列时单独评审迁移。 |
| 新旧事实冲突 | 保留时间/Confidence，不按热度裁决。 |

