# PR 08：Embedding 迁移与安全发布

## 1. 文档状态

- 状态：待评审
- 前置依赖：PR 01～PR 07
- 阻塞范围：新检索链路成为生产默认
- 运行时行为变化：由 Active Index Version 控制

## 2. 背景

当前 Memory 和 File Chroma Collection 使用 Chroma 隐式默认 Embedding。模型、Revision、Dimension、Normalization 和中文适用性都不是 Athena 的显式配置。直接修改已有 Collection 可能造成向量维度不兼容和不可控排序变化。

本 PR 显式定义 Embedding Profile，构建版本化索引，在不替换 Active Index 的情况下重建数据，并通过 Shadow Comparison、灰度和可回滚切流上线。

## 3. 目标

1. 固定 Embedding Model、Revision 和预处理方式。
2. Text 与 Code 支持不同 Profile。
3. 无停机重建现有数据。
4. 同一请求对比 Legacy 与 Planned 结果。
5. 通过配置/Registry 切换与回滚，不做破坏性原地修改。
6. 生产环境禁止未授权的模型下载。

## 4. 非目标

- 不只依据公开排行榜选模型。
- 切流时不删除 Legacy Collection。
- 不要求 Memory、PDF、Code、Table 共用同一模型。
- Shadow Result 未达门槛前不进入用户上下文。

## 5. Embedding Profile

```python
@dataclass(frozen=True)
class EmbeddingProfile:
    profile_id: str
    backend: str
    model: str
    revision: str
    dimension: int
    normalize: bool
    query_prefix: str
    document_prefix: str
    max_tokens: int
    content_types: tuple[str, ...]
```

示例：

```text
memory-multilingual-v1
document-multilingual-v1
code-v1
```

索引建立后 Profile 不可变。任何字段变化都创建新 Profile 和 Index Version。

## 6. 模型评估

使用 PR 01 切片对比：

- 适合中英文的多语言文本模型，例如 BGE-M3。
- 当前 Chroma 默认实现作为 Legacy Control。
- Code-oriented Embedding 用于代码行为和 Symbol 查询。

评价维度：Recall@30、nDCG@10、Hybrid 后 Exact Regression、目标硬件吞吐/延迟、内存/磁盘占用、最大输入长度、截断行为、License 和离线部署要求。

Memory 和 PDF 可以选择同一多语言模型，但 Code 必须独立决策。Excel 聚合的正确性不依赖 Embedding。

## 7. 显式集成

具体 Embedding Backend 位于 infrastructure。Core 只依赖 Port，不导入模型包。Chroma 可采用两种实现之一：

1. 打开 Collection 时传入显式 Embedding Function。
2. Infrastructure 预先计算向量，再显式写入/查询 Chroma。

无论采用哪种，Index 与 Query 必须使用同一 Profile，写入前校验 Dimension。

生产启动过程：

- 解析 Profile。
- 验证模型文件和 Revision 已存在。
- 验证 Collection Metadata 与 Profile 一致。
- Planned Index 不匹配时 Fail Closed。
- Legacy Retrieval 继续作为 Fallback。
- 除非开发配置明确允许，否则禁止运行时下载模型。

## 8. Collection 命名与 Metadata

```text
athena_memory__legacy
athena_memory__memory_multilingual_v1
athena_file_text__document_multilingual_v1
athena_file_code__code_v1
```

Collection Name 使用 Chroma 兼容 ASCII。Metadata 至少包含：

```json
{
  "logical_source": "memory",
  "index_version": "memory_multilingual_v1",
  "embedding_profile_id": "memory-multilingual-v1",
  "embedding_model": "...",
  "embedding_revision": "...",
  "dimension": 1024,
  "chunker_version": "memory-v1",
  "created_at": "..."
}
```

## 9. Index Registry

新增表而不是修改现有 Memory/File 表：

```text
retrieval_indexes
  id, logical_source, index_version, profile_json,
  status, document_count, active, created_at, updated_at

retrieval_reindex_jobs
  id, index_id, cursor, processed, failed,
  status, error_message, created_at, updated_at
```

SQLAlchemy `create_all` 可以为已有安装创建新表，无需引入通用 Runtime Migration Hook。

状态机：

```text
building -> validating -> shadow -> active -> retired
                         \-> failed
```

每个 Logical Source 只能有一个 Active Index；Shadow 永不直接服务用户。

## 10. Reindex Workflow

1. 创建 Versioned Collection 和 `building` Registry。
2. 按稳定 ID 顺序从 SQLite Source of Truth 分页读取。
3. 使用目标 Chunker Profile 重建 Chunk。
4. 分批 Embedding 和幂等写入。
5. 每批成功后持久化 Cursor/Count。
6. 校验 Count、Dimension、Metadata 和 Sample Query。
7. 标记为 `shadow`。
8. 执行 Offline + Online Shadow Compare。
9. 评审通过后原子切为 Active。

Vector ID 由 Source Item ID、Chunk Profile、Ordinal 确定性生成，批次重试不得产生重复向量。

## 11. 迁移期间写入

- SQLite 始终是 Source of Truth。
- 正常请求继续写 Active Index。
- 使用 Change Journal 或 Dual-write Queue 记录 Snapshot 后的变更。
- Validation 前把增量变更 Replay 到 Building Index。
- Delete/Update 通过显式补偿写 Active 和符合条件的 Shadow。
- Shadow 写失败只能记录和重试，不能让正常请求失败。

## 12. Shadow 模式

```text
legacy  -> 查询并返回 Active Legacy
planned -> 查询并返回 Planned
shadow  -> 返回 Active；在预算内异步查询 Planned
```

记录 Candidate Overlap、Rank Correlation、Selected ID 差异、Abstention 差异、Latency、Error Rate 和额外成本。

Shadow 结果不得注入 LLM，也不得增加 Memory Access Count。

## 13. 切流门槛与过程

必须同时满足：

- Overall nDCG@10 相对原始基线提升至少 10%。
- Exact/API/Error Code Hit@3 至少 95%。
- 标注集 Recall@30 至少 90%。
- Expected-empty 误召回率不超过 5%。
- 全部 Locator Check 通过。
- Shadow Error Rate 低于约定门槛。
- P95 Latency 在 Route Budget 内。
- Reindex 没有遗漏 Live Source Record。

切流步骤：冻结 Profile/Prompt/Index Version，Replay 最后增量，运行最终离线评估，小比例 Session 开启 Planned，逐级放量，最后更新 Active Registry。Legacy Index 保留完整回滚窗口。

## 14. 回滚

将 Active Registry/配置指针切回旧 Index，并按需关闭 Query Analyzer/Reranker Flag。事故处理中不得立即删除新 Collection；停止 Shadow Write 并保留用于诊断，回滚窗口结束后通过单独维护操作清理。

## 15. 文件改动

```text
athena/core/retrieval/indexes.py
athena/core/retrieval/embedding.py
athena/infrastructure/chroma/memory_store.py
athena/core/files/runtime.py
athena/infrastructure/sqlite/models.py
athena/infrastructure/sqlite/index_repository.py
athena/core/files/tasks.py
athena/config/settings.py
athena/main.py
tests/test_embedding_profiles.py
tests/test_reindex_workflow.py
tests/test_retrieval_shadow_mode.py
```

模型依赖优先放入 `pyproject.toml` Optional Extra，除非所有部署都必须安装。

## 16. 配置

```python
retrieval_pipeline_mode: str = "legacy"
retrieval_memory_index_version: str = "legacy"
retrieval_document_index_version: str = "legacy"
retrieval_code_index_version: str = "legacy"
retrieval_allow_model_download: bool = False
retrieval_shadow_timeout_ms: int = 5000
retrieval_reindex_batch_size: int = 100
```

外部 Provider Secret 继续使用现有 Settings 机制，禁止写入 Index Metadata。

## 17. 测试与验收

测试覆盖：Profile/Dimension 不匹配时 Planned Fail Closed 且 Legacy 正常；相同 Profile 产生确定性 Collection Identity；Reindex 可续跑；批次重试不重复；Build 期间写入在 Validation 前 Replay；Delete/Update 有补偿；Shadow 不返回结果也不计访问；Cutover 只切 Pointer；Rollback 无需 Rebuild；生产不下载缺失模型；所有 Index Version 维持 Session/Attachment 隔离。

验收要求：模型、Revision、Dimension、预处理和 Index Version 全部显式可观测；旧数据无破坏重建；Active/Shadow 安全共存；全部质量门槛通过；回滚路径经过自动化测试。

## 18. 风险

| 风险 | 缓解措施 |
| --- | --- |
| 中文提升但 Code 下降 | Source-specific Profile 和切片门槛。 |
| Reindex 漏掉并发更新 | Snapshot + Change Journal Replay。 |
| 临时索引占用翻倍 | Build 前容量检查，切流后按保留策略清理。 |
| 生产模型不可用 | 预置、启动校验、Legacy Fallback。 |
| Shadow 计算翻倍 | Sampling、Deadline、结果不进入用户链路。 |

