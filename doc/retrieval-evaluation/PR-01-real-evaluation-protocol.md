# PR-01：建立真实检索评估标准和标准答案

## 先说结论

这份 PR 先把“如何测试检索系统”这件事定义清楚：

1. 准备一批真实但已经脱敏的数据。
2. 为每个问题标出哪些内容应该被找到，哪些内容绝对不能返回。
3. 规定如何计算召回率、命中率、排序质量和误报率。
4. 规定数据如何版本化、如何审计，以及如何避免泄露敏感信息。

完成后，团队有了一份可以反复使用的“标准题库”和“评分规则”。PR-02 会用真实运行时执行这份题库，PR-03 会根据分数做 CI 门禁，PR-04 会把线上发现的问题补充回题库。

## 1. 状态和边界

- 状态：待评审
- 已有前置能力：`RetrievalTrace`、`HybridRetrievalManager`、`FileIntelligenceRuntime`
- 后续 PR：PR-02、PR-03、PR-04
- 本 PR 不改变线上检索的排序结果

## 2. 为什么要做

现在的评估测试会使用 `FixtureLegacyPipeline`。它还会把真实的 SQLite、Chroma、文件解析器和 LLM 换成测试替身。

这种测试可以回答“指标公式和 trace 接口有没有写错”，但不能回答以下问题：

- 真实 PDF 和 OCR 是否解析正确？
- Excel 的表格位置是否保留？
- 代码文件的路径、符号和行号是否保留？
- Embedding 和查询扩展能否找到真正相关的内容？
- 问题出在解析、索引、候选召回、结果融合，还是最终排序？

因此，本 PR 建立正式的评估数据模型和可审计的标准答案。Fixture 测试继续保留，用来做单元测试，但不能再被当作线上检索质量的证据。

## 3. 这次要做什么

### 3.1 目标

- 把评估数据、运行快照、检索结果和指标定义成稳定、可版本化的协议。
- 使用“文档别名 + 内容位置”标注相关性，而不是直接记录运行时的 `chunk_id`。
- 支持 memory、text、PDF、Excel、code、image 六类数据源。
- 支持多人标注、标注版本、争议处理和脱敏审计。
- 把现有的纯函数指标迁移到正式的 `athena/evaluation` 包。

### 3.2 不在本 PR 中做

- 不创建真实的 SQLite/Chroma 快照，见 PR-02。
- 不设置 CI 阈值，也不做 baseline 比较，见 PR-03。
- 不接入线上流量和反馈界面，见 PR-04。
- 不用“最终回答是否正确”替代检索召回率。最终回答和检索结果是两个不同问题。

## 4. 先理解几个名词

- **Fixture**：测试替身。它能让测试稳定、快速，但不代表真实生产环境。
- **金标数据集**：人工确认过的标准答案集合，用来和系统结果比较。
- **locator**：内容位置，例如 PDF 第 12 页、Excel 某个 sheet 的第 20 行、代码文件的第 10 到 15 行。
- **chunk**：系统把文档切成的小片段。重新切分文档后，chunk ID 可能变化，所以标准答案不能依赖它。
- **gain**：相关程度。数值越高，表示内容越值得排在前面。

## 5. 目录和模块

```text
athena/evaluation/
├── __init__.py
├── models.py              # 定义数据集、问题和标准答案的数据结构
├── dataset.py             # 加载并校验 JSONL/manifest
├── relevance.py           # 根据位置和片段 hash 判断是否命中
├── metrics.py             # 计算召回率、命中率、排序质量和延迟
└── privacy.py             # 脱敏以及安全地生成报告

retrieval_eval/datasets/production-sanitized-v1/
├── manifest.json
├── cases.jsonl
├── memories.jsonl
└── corpus/                # 脱敏后的原始文件，不提交普通代码仓库
```

`tests/retrieval_eval/metrics.py` 中的公式测试继续保留。正式评估代码不能依赖 `tests/retrieval_eval/fixtures.py`。

## 6. 数据如何定义

### 6.1 数据集清单 manifest

manifest 是整个数据集的身份证，至少包含：

```json
{
  "dataset_id": "production-sanitized",
  "version": "v1",
  "created_at": "2026-08-20T00:00:00Z",
  "redaction_policy": "v1",
  "case_count": 290,
  "content_manifest_sha256": "...",
  "label_schema_version": "1",
  "min_annotators": 2
}
```

每次评估报告都必须记录 manifest 的 hash。数据集一旦被使用，后续修改必须创建新版本，不能直接覆盖旧版本。

### 6.2 一个评估问题

```json
{
  "case_id": "contract-termination-014",
  "query": "提前终止时有哪些付款风险？",
  "scope": {
    "source": "file",
    "session_alias": "legal-17",
    "attachment_aliases": ["msa-2025"]
  },
  "relevant": [
    {
      "document_alias": "msa-2025",
      "locator": {"page": 12},
      "quote_sha256": "...",
      "gain": 3,
      "rationale": "第 4.2 节直接回答问题"
    }
  ],
  "must_not_return": [],
  "expect_empty": false,
  "labels": ["pdf", "natural_language", "exact_locator"],
  "annotation": {
    "status": "approved",
    "annotator_ids": ["annotator-a", "annotator-b"],
    "reviewed_at": "2026-08-20T00:00:00Z"
  }
}
```

每个字段的含义是：

- `case_id`：问题的唯一编号，不能重复。
- `scope`：问题允许检索的范围。
- `relevant`：应该找到的内容及其位置。
- `gain`：内容的相关程度。
- `must_not_return`：明确不能返回的内容。
- `expect_empty`：这个问题是否应该没有结果。
- `labels`：用于后续分组统计的标签。
- `annotation`：标注人和审核状态。

不同数据源有不同的定位要求：

- memory 使用 `memory_alias`。
- 文件使用 `attachment_alias`。
- 代码必须包含 `path`、`start_line` 和 `end_line`。
- Excel 必须包含 `sheet` 和行范围。
- PDF 必须包含 `page`。

`quote_sha256` 只保存脱敏片段的 hash，不把片段正文写进报告。

### 6.3 如何从标准答案对应到真实 chunk

真实索引建立后，PR-02 会生成只存在于评估 workspace 中的 `bindings.json`：

```json
{
  "attachment_aliases": {"msa-2025": "01J..."},
  "memory_aliases": {"decision-db": "01K..."},
  "locator_to_chunk_ids": {
    "msa-2025#page=12": ["01M..."]
  }
}
```

标准答案永远不直接保存 `chunk_id`。评估器通过别名和 locator 找到实际 chunk，并判断检索结果是否覆盖了标准答案的位置。这样重新切分或重新索引后，标准答案仍然有效。

## 7. 怎么评分

评估分两个阶段：候选阶段和最终排序阶段。

### 7.1 候选阶段

输出 `Recall@10/30/50`，意思是：前 10、30、50 个候选结果中，找到了多少标准答案。

例如一个问题有 4 个相关片段，前 10 个候选找到了 3 个，那么 `Recall@10` 就是 75%。

### 7.2 最终排序阶段

输出：

- `Hit@1/3`：前 1 或前 3 个结果中是否出现相关内容。
- `MRR`：第一个相关结果排得越靠前，分数越高。
- `nDCG@10`：综合考虑前 10 个结果的位置和相关程度。
- `Precision@5`：前 5 个结果中有多少是真正相关的。

### 7.3 负向问题

对于本来就不应该返回内容的问题，输出：

- `empty_false_positive_rate`：应该为空却返回了内容的比例。
- `harmful_hit_rate`：返回了明确禁止返回内容的比例。
- 非空问题的漏召回率。

空的 `relevant` 用例不参与 Recall 和 MRR 的平均值，只用于负向指标。

所有指标都要能按以下维度拆开查看：总体、数据源、query label、模型版本和索引版本。文件数据还要按具体定位类型统计，例如 PDF 页码、Excel sheet/row/value、代码 path/symbol/line，以及 memory 的 fact/summary/session。

## 8. 隐私和标注质量

- 原始文件和 Query 只能进入加密评估存储，不能进入普通日志或 Git。
- 日志只记录 hash、别名、ID、长度、分数和 locator。
- `gain=3`、`must_not_return` 和 `expect_empty=true` 的用例必须有人工理由。
- 每条用例至少由一名标注人独立标注。
- 脱敏后必须扫描密钥、邮箱、手机号、文件路径和用户 ID。

## 9. 文件改动

```text
athena/evaluation/__init__.py
athena/evaluation/models.py
athena/evaluation/dataset.py
athena/evaluation/relevance.py
athena/evaluation/metrics.py
athena/evaluation/privacy.py
retrieval_eval/datasets/.../manifest.json
retrieval_eval/datasets/.../cases.jsonl
tests/test_evaluation_dataset.py
tests/test_evaluation_metrics.py
```

## 10. 测试和验收标准

以下情况必须拒绝加载数据集：非法 locator、重复 `case_id`、未知 label、未审核的 `gain=3` 用例。

还必须验证：

- 同一个数据集 hash 重复加载时，结果稳定。
- 相关性判断覆盖 PDF page、Excel sheet/row、代码 path/line 和 quote hash。
- 报告序列化后不包含正文、原始 Query、密钥或未脱敏字段。
- 至少完成一份真实脱敏数据集的标注审计。

## 11. 风险和回滚

标注规则变化时，通过 `label_schema_version` 和新的数据集版本回滚或切换。指标代码即使有问题，也只会影响评估报告，不会改变生产检索。
