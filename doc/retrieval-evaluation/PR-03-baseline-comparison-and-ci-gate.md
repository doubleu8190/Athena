# PR-03：比较新旧版本，并在 CI 中拦截质量退化

## 先说结论

PR-01 定义了标准答案，PR-02 用真实生产流程跑出了结果。本 PR 把这些结果变成可审计的报告，并回答：

> 新版本的检索效果，是否比旧版本更差？差多少？是否应该让 CI 失败？

比较必须在同一批问题、同一个数据快照和相同运行条件下进行。这样差异才主要来自检索代码或配置变化，而不是测试数据变化。

## 1. 状态和边界

- 状态：待评审
- 前置依赖：PR-01、PR-02
- 后续 PR：PR-04
- 新增离线评估和 CI 命令，不改变服务默认排序

## 2. 要解决的问题

只看新版本自己的分数还不够，因为没有参照物。新版本 Recall 是 90%，到底是进步还是退步，需要和旧版本在完全相同条件下的结果比较。

本 PR 支持：

- 运行 legacy（旧流程）和 candidate（待验证的新流程）。
- 为每个问题保存两套结果。
- 计算成对差值和统计置信区间。
- 生成 JSON、Markdown 和机器可读报告。
- 按规则决定 CI 通过、失败或仅警告。

门禁只使用真实金标和真实 trace，不能用 fixture 得出的理想分数作为阈值依据。

## 3. 如何保证比较公平

```text
同一数据集 + 同一 snapshot
             |
      +------+------+
      |             |
    legacy       candidate
      |             |
      +------+------+
             v
        成对比较
             |
       报告 + 门禁结果
```

legacy 和 candidate 必须共享：

- 原始文件。
- SQLite 和 Chroma 快照。
- Query 集合。
- 模型版本。
- 评估顺序。

如果 candidate 使用了新的索引，就必须创建新的 snapshot version，并在报告中明确标注，不能把两个不同索引假装成同一条件。

## 4. 模块和结果格式

```text
athena/evaluation/
├── executor.py            # 执行真实用例并保存结果
├── report.py              # 生成 JSON/Markdown/CSV 报告
├── comparison.py          # 计算成对差值和 bootstrap 置信区间
├── gate.py                # 判断门禁规则
└── cli.py                 # 提供 run、compare、gate 命令
```

每个 case 的结果至少包含：case ID、pipeline 名称、候选 ID、最终结果 ID、locator、trace 摘要和错误分类。

结果不能写候选正文；同时必须记录 dataset、snapshot、commit、model config 和 index version hash，方便追溯。

## 5. 报告要回答什么

报告至少包含以下内容：

- **总体质量**：Recall、Hit、MRR、nDCG、Precision。
- **分组质量**：按 label、数据源和文件类型查看质量、locator、空结果和有害命中。
- **检索路线**：keyword、vector、multi-query 各自带来了哪些候选，彼此有多少重叠。
- **耗时分布**：query expansion、candidate retrieval、fusion、selection 各阶段以及总耗时的 P50/P95。
- **成本**：LLM 调用次数、token 数、Embedding 调用次数、失败次数和重试次数。
- **失败清单**：没有进入候选、进入候选但排序靠后、locator 错误、索引缺失和运行时异常。

这样报告不只告诉我们“分数下降了”，还可以定位下降发生在哪类问题和哪个处理阶段。

## 6. 怎么做统计比较

每个 case 都要保存 legacy 和 candidate 的成对结果，然后使用 paired bootstrap 计算 95% 置信区间。建议重复抽样 10,000 次，并固定随机种子，保证同一输入重复运行时结果稳定。

报告必须同时展示：

- 绝对差值，例如 `+0.02`。
- 相对差值，例如 `+5%`。
- 95% 置信区间。

不能只展示一个总体平均数。

最少需要比较：

```text
candidate.recall_at_30
final.hit_at_1
final.hit_at_3
final.mrr
final.ndcg_at_10
locator_accuracy
negative.empty_false_positive_rate
latency.p95
llm.calls_per_case
```

## 7. CI 门禁规则

默认规则放在版本化的 `evaluation-gate.yaml` 中：

```yaml
metrics:
  candidate.recall_at_30:
    max_regression: 0.01
  final.hit_at_3:
    max_regression: 0.01
  locator_accuracy:
    max_regression: 0.01
  negative.empty_false_positive_rate:
    max_increase: 0.0
  latency.p95:
    max_relative_increase: 0.20
labels:
  exact_identifier:
    final.hit_at_1:
      max_regression: 0.01
  error_code:
    final.hit_at_3:
      max_regression: 0.01
```

例如，`max_regression: 0.01` 表示该指标最多下降 0.01；`max_relative_increase: 0.20` 表示延迟最多相对增加 20%。

规则只有在以下条件都满足时才生效：

- 该 label 的 case 数达到最低样本数。
- 所有关键 case 都执行成功。
- 索引完整可用。

样本太少、索引不完整或模型调用失败，不能被当作“通过”。

关键负向指标变差，或精确查询超过阈值，直接让门禁失败；其他退化可以标记为 warning。若确实需要绕过门禁，必须提供 `--override-reason`，并写入审计记录。

## 8. 命令行和产物

```bash
python -m athena.evaluation.cli run \
  --workspace .run/retrieval-evaluation/eval-001 \
  --pipeline legacy \
  --output .run/retrieval-evaluation/reports/legacy.json

python -m athena.evaluation.cli compare \
  --baseline .run/retrieval-evaluation/reports/legacy.json \
  --candidate .run/retrieval-evaluation/reports/candidate.json \
  --output .run/retrieval-evaluation/reports/compare.json

python -m athena.evaluation.cli gate \
  --comparison .run/retrieval-evaluation/reports/compare.json \
  --rules doc/retrieval-evaluation/evaluation-gate.yaml
```

输出包括 JSON、Markdown 和机器可读的 `gate-result.json`。`.run/` 只保存生成物，不提交到版本库；长期保存的报告只包含脱敏字段和 manifest hash。

## 9. CI 如何运行

1. **Pull Request smoke**：使用小型真实脱敏 snapshot，检查 runtime、绑定关系和关键 label，目标是在 10 分钟内完成。
2. **Nightly/full**：使用完整 snapshot、全部用例和真实模型，上传报告，并与最近的 baseline 比较。

PR 阶段不能下载未审批的模型，也不能访问生产目录。真实模型密钥通过 CI secret 注入，日志必须关闭 raw query。

## 10. 文件改动

```text
athena/evaluation/executor.py
athena/evaluation/report.py
athena/evaluation/comparison.py
athena/evaluation/gate.py
athena/evaluation/cli.py
doc/retrieval-evaluation/evaluation-gate.yaml
tests/test_evaluation_comparison.py
tests/test_evaluation_gate.py
.github/workflows/retrieval-evaluation.yml
```

## 11. 测试和验收标准

- legacy 和 candidate 在同一个 snapshot 上运行，并且 case 顺序可复现。
- 固定 seed 时 bootstrap 结果稳定；样本不足时必须失败。
- 每条退化都能定位到 case、label、处理阶段和 trace。
- 门禁失败返回非零退出码；通过返回 0。
- 报告不包含正文、密钥或未脱敏 Query。
- 使用真实 snapshot 完成一次 legacy 报告和一次 candidate 对比。

## 12. 风险和回滚

门禁规则独立放在 YAML 中，因此可以不改检索代码就调整或回滚阈值。报告 schema 通过版本号递增，旧报告仍然可以比较。统计实现出错只会影响评估结果，不会改变线上流量。
