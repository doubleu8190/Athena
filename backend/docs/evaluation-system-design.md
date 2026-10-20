# Athena 评测系统设计

## 1. 目标和边界

Athena 是带知识库、长期记忆、工具调用和多 Agent 编排的系统。评测系统需要回答四个问题：

1. 检索是否找到了正确的内容。
2. 模型是否基于正确内容给出了正确、完整、可引用的答案。
3. Agent 是否选择了正确的工具并按约束完成任务。
4. 版本、配置或 Prompt 变化后，质量、延迟、Token 和成本是否回归。

评测系统本身不参与线上回答，不修改业务检索结果。它读取一个版本化数据集，固定一份运行配置，执行被测系统，保存完整证据和逐题结果，然后计算指标和门禁结论。

## 2. 总体架构

```text
数据集/案例
    |
    v
评测编排器 -- 固定 provider、model、prompt、index、代码版本
    |
    v
被测入口（retrieval / chat / agent）
    |
    +--> retrieval_runs + retrieval_candidates
    +--> messages / agent_runs / tool_calls / events
    |
    v
评测器
    +--> 确定性指标（Recall、MRR、Schema、规则）
    +--> LLM Judge（正确性、相关性、忠实性、完整性）
    +--> 成本/性能采集
    |
    v
实验结果、逐题证据、对比、回归门禁
```

评测运行必须是不可变的。每次运行记录 Git commit、数据集版本、代码配置、模型和 Prompt 版本、embedding/index generation、评测器版本、开始结束时间。这样同一个 run 可以重放和审计。

## 3. 评测层次

### 3.1 检索评测

适合当前已有的 `backend/domain/retrieval/evaluation.py`，并扩展到知识库、附件和记忆三类 scope。

| 维度 | 指标 | 说明 |
| --- | --- | --- |
| 召回 | Recall@1/3/5/10 | 标注相关来源被召回的比例 |
| 命中 | HitRate@K | Top-K 中是否至少有一个相关来源 |
| 排序 | MRR、nDCG@K | 第一个相关结果位置和多相关结果排序质量 |
| 上下文 | Context Precision/Recall | 实际注入上下文的精确度和覆盖率 |
| 运行 | P50/P95 latency、timeout rate | 检索耗时和失败比例 |

相关性标注建议支持等级：`0=不相关`、`1=部分相关`、`2=完全相关`。只有二值标注时继续使用现有 `relevant_source_ids`；有等级时用 graded nDCG。

### 3.2 答案评测

每道题可以同时配置确定性参考答案和可接受答案要点。指标按任务类型选择，不强行使用一个指标覆盖所有问题。

| 任务类型 | 首选指标 |
| --- | --- |
| 分类/路由 | accuracy、macro-F1、confusion matrix |
| 数值/结构化输出 | exact match、字段级匹配、JSON Schema |
| 短答案 | exact match、token F1 |
| 长答案/RAG | LLM Judge：correctness、groundedness、completeness、relevance |
| 引用回答 | citation precision、citation recall、citation entailment |

LLM Judge 不直接返回一个自由文本分数。要求结构化输出：

```json
{
  "correctness": {"score": 4, "reason": "...", "evidence": ["case.reference[0]"]},
  "groundedness": {"score": 3, "reason": "...", "evidence": ["retrieval_candidates:c1"]},
  "completeness": {"score": 4, "reason": "...", "evidence": []},
  "pass": true
}
```

Judge Prompt、Judge model、temperature、schema 和解析失败都写入结果。高风险门禁不应只依赖单次 LLM Judge：关键集使用双 Judge 或人工复核抽样，并记录 Judge agreement。

### 3.3 Agent 行为评测

从现有 `agent_runs`、`tool_calls` 和事件中计算：

- task success：是否达到案例的成功条件；
- tool selection accuracy：是否调用了允许且必要的工具；
- argument validity：参数是否符合 schema 和案例约束；
- approval compliance：需要审批时是否等待/拒绝，禁止副作用是否发生；
- unnecessary calls、step count、retry count；
- latency、input/output tokens、estimated cost。

工具评测必须使用隔离 sandbox 和 stub，禁止让评测集对真实外部系统产生副作用。案例可声明 `allowed_tools`、`required_tools`、`forbidden_tools` 和 `expected_effects`。

### 3.4 线上质量和回归

离线集解决可重复性，线上采样解决分布变化。线上只保存脱敏后的输入、检索证据、输出和成本；用户反馈或人工标注进入新的数据集版本，不直接改写历史结果。

建议每日运行 smoke 集、每次 Prompt/模型/检索配置变更运行 regression 集、发布前运行 full 集。线上指标只作为监控和采样来源，不与离线分数直接混合。

## 4. 数据模型

推荐新增以下表；JSON 字段用于扩展，核心筛选字段保持结构化。表名可按现有 SQLAlchemy/PostgreSQL 模型风格实现。

### `eval_datasets`

数据集逻辑实体：`dataset_id`、`name`、`description`、`task_type`、`owner`、`created_at`。

### `eval_dataset_versions`

不可变版本：`version_id`、`dataset_id`、`version`、`manifest_sha256`、`case_count`、`status`、`created_at`。

### `eval_cases`

单题定义：

```json
{
  "case_id": "rag-cn-001",
  "dataset_version": "rag-cn@2026-10-18",
  "task_type": "rag_answer",
  "input": {"message": "中文全文搜索如何实现？", "session_id": null},
  "retrieval": {
    "scope": "knowledge",
    "relevant_sources": [{"source_id": "db", "grade": 2}]
  },
  "answer": {
    "reference": "使用 PostgreSQL 和中文全文检索。",
    "must_include": ["PostgreSQL", "中文全文检索"],
    "must_not_include": ["虚构的数据库"]
  },
  "agent": {
    "allowed_tools": [],
    "required_tools": [],
    "forbidden_tools": ["shell"]
  },
  "tags": ["chinese", "retrieval", "smoke"],
  "metadata": {"difficulty": "easy", "owner": "team-a"}
}
```

### `eval_runs`

一次实验运行：`run_id`、`dataset_version_id`、`mode`（`retrieval/chat/agent`）、`status`、`git_sha`、`config_json`、`model_snapshot_json`、`judge_config_json`、`baseline_run_id`、`started_at`、`completed_at`、`error_message`。

### `eval_case_results`

逐题结果：`run_id`、`case_id`、`status`、`input_snapshot_json`、`output_text`、`retrieval_run_id`、`agent_run_id`、`trace_json`、`duration_ms`、`input_tokens`、`output_tokens`、`estimated_cost`、`error_message`。

### `eval_metric_values`

指标明细：`run_id`、`case_id`（允许为空表示汇总）、`metric_name`、`value_numeric`、`value_json`、`evaluator_version`、`evidence_json`。

### `eval_gates`

门禁规则和结论：`gate_id`、`run_id`、`rule_json`、`status`、`failed_metrics_json`、`created_at`。

评测表和业务运行表通过 `retrieval_run_id`、`agent_run_id` 关联，避免复制检索候选和工具轨迹。评测输入和输出快照仍需保存，保证后续业务数据被清理后可以解释历史结果。

## 5. 数据集格式和生命周期

建议先采用 JSONL，便于 Git review；稳定后再导入数据库。每行一个 case，manifest 记录文件 hash 和 schema version。

```text
draft -> reviewed -> frozen -> deprecated
```

- `draft`：作者可编辑；
- `reviewed`：通过标注检查和抽样复核；
- `frozen`：可运行，内容不可变，只能创建新版本；
- `deprecated`：不再用于门禁，但历史 run 仍保留。

数据集检查器至少校验：case_id 唯一、引用 source 存在、相关来源非空、结构化参考答案符合 schema、工具约束没有冲突、语言/标签统计可用。

## 6. 执行流程

```text
创建 eval_run
  -> 解析并冻结配置
  -> 按 case 执行（默认并发，带 rate limit 和 timeout）
  -> 收集业务 trace
  -> 运行确定性 evaluator
  -> 批量运行 Judge evaluator
  -> 写入 case result 和 metric values
  -> 聚合均值、分位数、置信区间
  -> 与 baseline 对比
  -> 执行 gates
  -> 标记 passed / failed / partial
```

每题需要独立 timeout、重试上限和幂等 key。重试不能覆盖首次失败，应该保存 attempt 记录并在汇总中区分 `first_attempt`、`final_attempt` 和 `flaky`。

聚合结果至少包含样本数、均值、P50/P95、通过率和 95% bootstrap 置信区间。小样本集只显示分数和样本数，不宣称统计显著性。

## 7. 评测配置示例

```yaml
name: rag-regression
dataset: rag-cn@2026-10-18
mode: chat
target:
  provider: openai
  model: gpt-5
  prompt_version: system@42
  retrieval_config:
    candidate_k: 30
    rerank_k: 10
    context_k: 5
evaluators:
  - retrieval_metrics
  - answer_rules
  - groundedness_judge
  - cost_latency
gates:
  - metric: retrieval.recall_at_5
    op: ">="
    value: 0.85
  - metric: answer.groundedness.pass_rate
    op: ">="
    value: 0.90
  - metric: runtime.p95_latency_ms
    op: "<="
    value: 15000
  - metric: runtime.estimated_cost_per_case
    op: "<="
    value: 0.05
  - metric: regression.recall_at_5_delta
    op: ">="
    value: -0.02
```

门禁应支持绝对阈值和相对 baseline 阈值。建议把“质量下降超过阈值”设为阻断条件，把成本和延迟设为警告或阻断条件，按发布类型配置。

## 8. API 设计

新增独立的 `/api/evaluations` 资源，不把评测接口塞进 `/api/retrieval`。

```text
GET    /api/evaluations/datasets
POST   /api/evaluations/datasets
GET    /api/evaluations/datasets/{id}/versions
POST   /api/evaluations/datasets/{id}/versions
GET    /api/evaluations/runs
POST   /api/evaluations/runs
GET    /api/evaluations/runs/{run_id}
POST   /api/evaluations/runs/{run_id}/cancel
GET    /api/evaluations/runs/{run_id}/cases
GET    /api/evaluations/runs/{run_id}/metrics
GET    /api/evaluations/runs/{run_id}/compare?baseline_run_id=...
POST   /api/evaluations/runs/{run_id}/gates
```

`POST /runs` 返回 `run_id` 和 `status=queued`，执行走后台任务；进度通过现有 SSE 事件增加 `evaluation.started`、`evaluation.case_completed`、`evaluation.completed`、`evaluation.failed`。查询接口支持按 dataset、tag、status、metric 和时间范围过滤。

CLI 与 API 共用同一个 application service，避免 CLI 和网页产生两套评测逻辑：

```text
python -m backend.scripts.evaluate run --config configs/evals/rag-regression.yaml
python -m backend.scripts.evaluate compare --run RUN_ID --baseline BASELINE_ID
python -m backend.scripts.evaluate validate-dataset data/evals/rag-cn.jsonl
```

## 9. 前端页面

在现有 `RetrievalView` 旁新增 `EvaluationView`，分三层展示：

1. 数据集：版本、题数、标签分布、冻结状态、最近运行。
2. 实验：目标配置、进度、总览指标、baseline 差异、门禁结论。
3. 逐题详情：输入、输出、参考答案、检索候选、引用、Judge 理由、trace 链接和重跑按钮。

总览页同时显示质量、延迟、Token/成本和失败率。总分只能作为排序辅助，默认展示各维度指标和置信区间，避免一个加权分掩盖安全或忠实性问题。

## 10. 推荐实现顺序

### Phase 1：MVP（先做）

- 复用现有 `evaluate_rankings`，增加 JSONL 数据集 loader 和 dataset version hash；
- 新增 `eval_runs`、`eval_case_results`、`eval_metric_values`；
- 提供 CLI：validate、run、compare；
- 支持 retrieval metrics、exact/rule-based answer metrics、latency/cost；
- 输出 JSON 和 Markdown 报告；
- 在 CI 对 smoke 集做 Recall、失败率和延迟门禁。

### Phase 2：生成式答案与 Agent

- 增加统一 `Evaluator` 协议和 evaluator registry；
- 接入结构化 LLM Judge、证据引用和 judge 重试；
- 连接 `agent_runs/tool_calls/events`，实现工具约束和审批合规评测；
- 增加 PostgreSQL repository 和 `/api/evaluations` 查询接口。

### Phase 3：产品化

- 前端数据集/实验/逐题页面；
- baseline 对比、分组分析、bootstrap 置信区间；
- 线上脱敏采样、人工标注回流和自动生成回归集；
- 定时运行、模型/Prompt registry、成本预算和发布门禁。

## 11. 当前代码的落点

建议目录结构如下：

```text
backend/src/domain/evaluation/
  entities.py       # DatasetVersion、EvalRun、CaseResult、MetricValue、Gate
  ports.py          # runner、evaluator、repository ports
  metrics.py        # 确定性指标
backend/src/application/evaluation/
  service.py        # 创建/执行/查询/比较 run
  evaluators/       # retrieval、answer、agent、runtime
backend/src/infrastructure/evaluation/
  dataset_loader.py
  judge.py
  postgres_repository.py
backend/src/interfaces/http/controllers/evaluation_controller.py
backend/scripts/evaluate.py
backend/tests/evaluation/
```

现有 `retrieval_runs` 和 `retrieval_candidates` 保持为业务检索轨迹；评测运行只增加引用字段和聚合结果。这样既能复用当前中文 embedding 评测脚本，也不会把“线上一次检索发生了什么”和“离线实验得了多少分”混成同一个概念。

## 12. 最小验收标准

完成 MVP 后，下面的流程必须可重复：

1. 用同一个 dataset version 和 config 连续运行两次，配置快照和 case 数一致。
2. 任意一个汇总指标都能追溯到具体 case、retrieval run 或 agent run。
3. 删除或修改业务索引后，历史评测结果仍能显示原始输入、输出和证据。
4. baseline 质量下降超过阈值时，CLI 返回非零退出码，CI 能阻断发布。
5. 单题超时、Judge 解析失败、被测系统失败都不会让整个 run 静默成功；汇总状态为 `partial` 或 `failed` 并显示失败原因。
