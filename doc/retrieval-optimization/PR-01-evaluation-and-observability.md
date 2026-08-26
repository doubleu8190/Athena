# PR 01：召回评估基线与可观测性

## 1. 文档状态

- 状态：待评审
- 前置依赖：无
- 阻塞范围：PR 02～PR 08
- 运行时行为变化：不改变排序结果

## 2. 背景

Athena 当前存在两条相互独立的召回链路：

- `athena/core/memory/retrieval.py` 负责长期记忆召回。
- `FileIntelligenceRuntime.search_file()` 负责附件内容召回。

两条链路都缺少阶段化评估和统一追踪，当前无法准确回答：相关内容是否进入候选集、在哪个阶段丢失、哪个通道贡献了结果、融合是否改善排序，以及每个阶段增加了多少延迟和成本。

本 PR 只建立评估与追踪基础，不修改检索逻辑。

## 3. 目标

1. 建立可重复执行的离线召回评估集。
2. 分开衡量候选召回、最终排序和上下文选择。
3. 记录各通道原始分数、排名、耗时和降级原因。
4. 为后续 PR 定义统一的质量门槛。
5. 默认不在日志中写入完整用户查询或文档正文。

## 4. 非目标

- 不修改 Query Rewrite、RRF、阈值和 Top K。
- 不建设用户反馈 UI。
- 不依赖外部评估平台。
- 不用最终回答质量掩盖候选召回失败。

## 5. 评估数据协议

新增 `tests/retrieval_eval/cases.jsonl`，每行一条用例：

```json
{
  "id": "memory-preference-001",
  "query": "我之前选的前端框架是什么？",
  "scope": {"source": "memory", "attachment_ids": []},
  "query_labels": ["memory", "natural_language", "lookup"],
  "relevant": [
    {"item_id": "fixture:preferred_frontend", "gain": 3}
  ],
  "must_not_return": ["fixture:unrelated_database"],
  "expect_empty": false,
  "notes": "精确事实和等价摘要均可接受"
}
```

字段定义：

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定用例 ID。 |
| `query` | 送入检索器的用户查询。 |
| `scope` | memory 或受信任的附件范围。 |
| `query_labels` | 用于切片统计的多标签。 |
| `relevant` | 相关结果及 1～3 级相关度。 |
| `must_not_return` | 明确有害、矛盾或无关的结果。 |
| `expect_empty` | 是否应主动不召回。 |
| `notes` | 人工标注依据。 |

Fixture 必须使用确定性 ID，禁止把运行时生成的时间 ID 固化到数据集。

## 6. 数据集构成

首版准备 150～300 条用例，允许一条用例属于多个切片：

| 切片 | 最少数量 | 主指标 |
| --- | ---: | --- |
| 原子记忆事实 | 25 | Hit@3 |
| 对话摘要 | 20 | nDCG@10 |
| FAQ/概念问题 | 20 | Recall@30 |
| 错误码/版本号 | 20 | Hit@3 |
| API/Class/函数 | 25 | Hit@3 |
| 自然语言文档查询 | 25 | nDCG@10 |
| 复杂/多跳查询 | 20 | Recall@30 |
| 应返回空结果 | 20 | 误召回率 |
| PDF 页码/表格 | 20 | Locator 准确率 |
| Excel 值/聚合 | 20 | Hit@3、计算正确率 |

至少三分之一为中文查询。除合成 Fixture 外，应逐步加入脱敏后的真实失败案例。

## 7. 指标

评估报告同时输出总体和分切片指标：

- 候选阶段：`Recall@10/30/50`。
- 精确标识符：`Hit@1/3`。
- 首个相关结果：`MRR`。
- 最终排序：`nDCG@10`、`Precision@5`。
- 负样本：有害结果命中率、空结果误召回率和漏召回率。
- 引用：page、sheet/row、path/line 定位准确率。
- 性能：各阶段及总体 P50/P95 延迟。
- 成本：LLM 调用次数和估算 token 数。

回答忠实度在后续端到端测试中单独衡量，不能替代召回指标。

## 8. Trace 协议

在 core 层定义与具体日志厂商无关的结构：

```python
@dataclass
class RetrievalStageTrace:
    stage: str
    route: str
    query_index: int
    duration_ms: float
    result_count: int
    error_code: str | None = None

@dataclass
class RetrievalCandidateTrace:
    item_id: str
    route: str
    rank: int
    native_score: float | None
    fused_score: float | None
    selected: bool = False
```

一次请求结束后聚合为单条 `retrieval_completed` 结构化日志，至少包含：

```text
request_id, source_scope, query_labels, analyzer_mode,
routes, stage_durations_ms, candidate_counts, selected_ids,
score_components, fallback_reason, total_duration_ms
```

默认只记录 ID、分数、长度和加盐 Query Hash。仅本地开发显式开启后才记录原始 Query；永不记录完整候选正文。

## 9. 文件改动

```text
athena/core/retrieval/trace.py
athena/config/settings.py
tests/retrieval_eval/cases.jsonl
tests/retrieval_eval/fixtures.py
tests/retrieval_eval/metrics.py
tests/retrieval_eval/runner.py
tests/test_retrieval_observability.py
```

`athena/core/retrieval` 只能依赖 core model/port，不得导入 SQLite、Chroma 或具体日志实现。

## 10. 执行方式

```bash
python -m tests.retrieval_eval.runner \
  --cases tests/retrieval_eval/cases.jsonl \
  --pipeline legacy \
  --output .run/retrieval-eval/legacy.json
```

支持模式：

- `legacy`：当前生产链路。
- `planned`：后续新链路。
- `compare`：比较两份已有报告。

`.run/` 下的报告是生成物，不提交版本库。

## 11. 测试方案

- 使用人工构造排名验证 MRR、nDCG、Recall 和 Precision 公式。
- 验证空结果用例独立计数。
- 验证 Trace 不包含候选正文。
- 验证某一路失败时仍生成完整追踪和降级原因。
- 本地固定 Fixture 连续运行两次，结果顺序必须稳定。

## 12. 验收标准

- 评估集覆盖全部规定切片。
- 一条命令可生成 JSON 与 Markdown 基线报告。
- 候选召回和最终排序分开报告。
- 可以定位各阶段耗时、候选数及降级原因。
- 生产默认日志不包含完整记忆或附件内容。
- 本 PR 不改变任何线上检索结果。

## 13. 发布与回滚

新增 `retrieval_trace_enabled`，生产默认关闭，离线 Runner 默认开启。关闭配置即可回滚，不涉及数据迁移。

## 14. 风险

| 风险 | 缓解措施 |
| --- | --- |
| 评估集过度依赖合成数据 | 持续加入脱敏真实失败样本。 |
| 标注分歧 | gain=3 必须写理由，争议用例人工复核。 |
| 日志泄露内容 | 默认只写 ID、Hash、长度和分数。 |
| Embedding 导致结果不稳定 | Fixture 固定向量实现，并记录索引版本。 |

