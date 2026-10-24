# Athena 评测系统实施文档

本文把[评测系统设计](evaluation-system-design.md)转换成可执行的开发任务。本文列出的 P0-P5 已在当前工作树实现；其中生产运行仍通过 feature flag 控制，未配置真实 LLM、数据库或 sandbox 时使用可重复的 fixture/fake target。

## 1. 实施原则

1. 评测运行和线上运行分开。评测默认使用离线数据集、隔离配置和 stub/sandbox，不触发真实外部副作用。
2. 业务 trace 是证据，评测结果是解释。评测表引用 `retrieval_runs`、`retrieval_candidates`、`agent_runs` 和 `tool_calls`，不复制整条 trace。
3. 一个 run 创建后不可修改。重跑创建新 run；重试创建新的 attempt，不覆盖第一次失败。
4. CLI、HTTP 和未来前端都调用同一个 `EvaluationService`，不各自实现指标或门禁逻辑。
5. MVP 先用确定性指标，Judge 和 Agent 评测在稳定的 run/case/metric 数据模型上扩展。

## 2. 交付顺序

| 阶段 | 交付物 | 结束条件 |
| --- | --- | --- |
| P0 | domain/application 契约、JSONL loader、版本 hash | 数据集可校验，非法输入有明确错误 |
| P1 | Retrieval runner、确定性指标、Markdown/JSON 报告 | 中文检索集可重复运行并生成逐题结果 |
| P2 | PostgreSQL 持久化、baseline 对比、门禁 | CI 可根据退出码阻断回归 |
| P3 | HTTP 查询/启动 API、SSE 进度事件 | API 与 CLI 返回同一份 run 数据 |
| P4 | Chat/Agent runner、规则 evaluator、隔离工具 stub | 能评测答案、工具选择和审批合规 |
| P5 | LLM Judge、前端 `EvaluationView` | 可查看实验、逐题证据和 Judge 理由 |

P0-P2 构成 Retrieval 回归 MVP；P3-P5 已接入但默认关闭外部副作用。

## 3. P0：领域模型和数据集加载

### 3.1 新增目录

```text
backend/src/domain/evaluation/
  __init__.py
  entities.py
  errors.py
  ports.py
  metrics.py
backend/src/application/evaluation/
  __init__.py
  service.py
  dataset_service.py
backend/src/infrastructure/evaluation/
  __init__.py
  dataset_loader.py
  versioning.py
```

### 3.2 领域实体

`entities.py` 使用现有 dataclass 风格，所有实体使用 `frozen=True, slots=True`。核心类型如下：

```python
class EvaluationMode(StrEnum):
    RETRIEVAL = "retrieval"
    CHAT = "chat"
    AGENT = "agent"

class EvaluationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"

@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    task_type: str
    input: dict[str, Any]
    retrieval: RetrievalExpectation | None = None
    answer: AnswerExpectation | None = None
    agent: AgentExpectation | None = None
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    name: str
    dataset_version: str
    mode: EvaluationMode
    target: dict[str, Any]
    evaluators: tuple[str, ...]
    gates: tuple[GateRule, ...] = ()
    concurrency: int = 1
    timeout_seconds: float = 60.0
    max_attempts: int = 1
```

`RetrievalExpectation` 支持 `relevant_sources: tuple[RelevanceLabel, ...]`，其中 `grade` 为 0、1 或 2；为了兼容当前数据，`relevant_source_ids` 转换为 grade=2。`AnswerExpectation` 支持 `reference`、`must_include`、`must_not_include` 和可选 JSON Schema。`AgentExpectation` 支持 `allowed_tools`、`required_tools`、`forbidden_tools` 和 `expected_effects`。

`EvalRun`、`CaseResult`、`MetricValue`、`GateResult` 必须包含稳定 ID、状态、创建时间和错误信息。所有 score 使用 `float | None`，失败或未执行不能写成 0。

### 3.3 端口

`ports.py` 定义 Protocol，不依赖 SQLAlchemy、FastAPI 或具体 LLM SDK：

```python
class EvaluationRepository(Protocol):
    async def create_run(self, run: EvalRun) -> None: ...
    async def save_case_result(self, result: CaseResult) -> None: ...
    async def save_metric(self, metric: MetricValue) -> None: ...
    async def save_gate(self, gate: GateResult) -> None: ...
    async def get_run(self, run_id: str) -> EvalRun | None: ...
    async def list_runs(self, filters: EvaluationFilters) -> tuple[list[EvalRun], int]: ...

class EvaluationTarget(Protocol):
    async def execute(self, case: EvaluationCase, config: EvaluationConfig) -> ExecutionArtifact: ...

class MetricEvaluator(Protocol):
    name: str
    async def evaluate(self, case: EvaluationCase, artifact: ExecutionArtifact) -> tuple[MetricValue, ...]: ...
```

`ExecutionArtifact` 统一保存 output、ranking、retrieval_run_id、agent_run_id、trace、耗时、Token、成本和 error。这样 Retrieval runner、Chat runner、Agent runner 可以替换，`EvaluationService` 不需要知道内部执行方式。

### 3.4 JSONL loader 和版本 hash

`infrastructure/evaluation/dataset_loader.py` 实现：

- 按 UTF-8 读取，一行一个 JSON 对象；
- 忽略空行，拒绝重复 `case_id`；
- 把旧 `chinese_cases.json` 的 `relevant_source_ids` 转成新格式；
- 校验 `task_type`、输入字段、相关来源、grade、工具约束和 JSON Schema；
- 返回 `DatasetManifest`，包括 case 数、标签计数、schema version 和规范化 hash。

`versioning.py` 先按 `case_id` 排序，再对每条 JSON 使用稳定 key 顺序和无空格编码，最后以 `sha256` 计算 `manifest_sha256`。版本 ID 建议为 `{dataset_name}@{yyyy-mm-dd}-{short_hash}`；同一内容必须得到同一个 hash。

命令：

```bash
PYTHONPATH=backend/src .venv/bin/python -m scripts.evaluate validate-dataset \
  backend/tests/retrieval_eval/chinese_cases.json
```

校验失败返回退出码 2；成功打印 manifest JSON。

## 4. P1：Retrieval MVP

### 4.1 目标入口

先实现 `RetrievalEvaluationTarget`，不启动完整 Agent。它接收 case 的 query 和 scope，使用当前 embedding/index 配置执行检索，并返回完整排序和 `retrieval_run_id`。如果当前环境没有可用 PostgreSQL 或 embedding provider，测试使用内存 target；生产 CLI 明确报告不可用原因。

建议文件：

```text
backend/src/infrastructure/evaluation/retrieval_target.py
backend/src/application/evaluation/evaluators/retrieval.py
```

target 必须记录：

- query、scope、candidate_k、rerank_k、context_k；
- 返回的 source ID 顺序；
- `retrieval_run_id` 和 index generation；
- duration、timeout、异常和候选缺失原因。

### 4.2 指标实现

在 `domain/evaluation/metrics.py` 实现确定性函数，不修改现有 `domain.retrieval.evaluation.evaluate_rankings` 的调用契约。新增函数通过 adapter 接受 `EvaluationCase` 和 `ExecutionArtifact`：

```text
retrieval.recall_at_1/3/5/10
retrieval.hit_rate_at_1/3/5/10
retrieval.mrr
retrieval.ndcg_at_1/3/5/10
runtime.duration_ms
runtime.timeout
runtime.error
```

要求：去重时保持首次出现顺序；case 没有相关来源时标记为 invalid，不计入正常分母；所有聚合结果同时保存 `sample_count` 和 `valid_count`。graded nDCG 使用 grade=0/1/2 计算 gain，二值数据仍得到与现有实现一致的结果。

### 4.3 编排服务

`application/evaluation/service.py` 的 `run()` 顺序固定为：

1. 加载并校验已冻结 dataset version。
2. 创建 `EvalRun(status=running)`，写入代码、模型、Prompt、索引和 evaluator 快照。
3. 按 concurrency 运行 case；每题使用 `case_id + run_id` 作为幂等 key。
4. 保存 `CaseResult`，再保存每个 evaluator 的 `MetricValue`。
5. 聚合 run-level metrics，执行 gate rules。
6. 按失败、部分成功或通过更新 run 状态。

并发实现先使用 `asyncio.Semaphore`；不要在 P1 引入新的任务队列。每题异常必须落为 case result，不能因为单题失败丢失整个 run 的其他结果。

### 4.4 报告

`backend/src/application/evaluation/reporting.py` 生成：

- `report.json`：完整 run、case、metric、gate 和错误；
- `report.md`：汇总表、分组指标、失败 case 和配置快照；
- `case-results.jsonl`：便于后续导入标注工具。

默认输出到 `data/evaluations/{run_id}/`；路径通过配置传入，禁止写入仓库目录。

## 5. P2：PostgreSQL 持久化和回归门禁

### 5.1 ORM 模型

新增 `backend/src/infrastructure/persistence/postgres/models/evaluation_models.py`，并在 `models/__init__.py` 导出。保持当前模型约定：配置、快照和证据使用 `Text` 存储 JSON，写入前使用共享 `dumps()`，读取使用 `loads()`。

核心表：

```text
eval_datasets
  dataset_id PK, name, description, task_type, owner, created_at, updated_at

eval_dataset_versions
  version_id PK, dataset_id FK, version, manifest_sha256 UNIQUE,
  status, case_count, manifest_json, created_at

eval_cases
  case_id PK, version_id FK, task_type, case_json, tags_json, created_at

eval_runs
  run_id PK, version_id FK, mode, status, name,
  git_sha, config_json, model_snapshot_json, judge_config_json,
  baseline_run_id, case_count, completed_count, failed_count,
  started_at, completed_at, error_message

eval_case_attempts
  attempt_id PK, run_id FK, case_id FK, attempt_number,
  status, input_snapshot_json, output_text, trace_json,
  retrieval_run_id, agent_run_id, duration_ms,
  input_tokens, output_tokens, estimated_cost, error_message,
  created_at, UNIQUE(run_id, case_id, attempt_number)

eval_metric_values
  metric_id PK, run_id FK, case_id FK NULL, attempt_id FK NULL,
  metric_name, value_numeric NULL, value_json, evaluator_version,
  evidence_json, created_at

eval_gates
  gate_id PK, run_id FK, rule_json, status,
  failed_metrics_json, created_at
```

索引：`eval_runs(status, created_at)`、`eval_runs(version_id)`、`eval_case_attempts(run_id, case_id)`、`eval_metric_values(run_id, metric_name)`、`eval_metric_values(case_id)`。case 删除采用冻结版本约束；已被 run 引用的版本不能物理删除。

将模型加入 `backend/src/infrastructure/persistence/postgres/models/__init__.py` 后，`Base.metadata.create_all` 会创建开发数据库中缺失的表和索引。它不会修改已有表字段，数据库结构以当前 ORM 模型为准。

### 5.2 Repository

新增 `backend/src/infrastructure/persistence/postgres/repositories/evaluation_repository.py`，实现 `EvaluationRepository`：

- 所有写入在 service 的一个 run 生命周期内使用同一个 session factory；
- `save_case_result` 使用 `(run_id, case_id, attempt_number)` 幂等；
- run-level metric 使用 `case_id IS NULL`；
- 查询返回 domain entities 或稳定 dict，不让 ORM 类型泄漏到 application；
- 读取详情时按 case_id 排序，指标按 metric_name 排序，保证 API 和报告稳定。

测试 fake 放到 `backend/tests/fakes/evaluation.py`，不把 PostgreSQL repository 直接用于 domain/application 单元测试。

### 5.3 Baseline 和 gate

`application/evaluation/gates.py` 实现：

```text
absolute: metric >= value / <= value / == value
delta:    current - baseline >= value / <= value
```

规则只允许比较 numeric metric；缺失值、invalid case、run failed 直接令 gate 为 `failed` 或 `blocked`，不能按 0 比较。门禁结果包含实际值、阈值、baseline 值、差值和失败原因。

CLI 退出码：

```text
0  run 完成且所有 required gates 通过
1  run 完成但 gate 失败
2  配置/数据集校验失败
3  run failed 或无法连接被测依赖
4  用户取消
```

## 6. CLI 实施

新增 `backend/scripts/evaluate.py`，使用 `argparse`，不要再复制 `evaluate_chinese_embedding.py` 的 provider 和指标逻辑。保留旧脚本作为兼容入口，内部改为调用新 service。

```bash
# 校验数据集
PYTHONPATH=backend/src .venv/bin/python -m scripts.evaluate validate-dataset DATASET.jsonl

# 运行
PYTHONPATH=backend/src .venv/bin/python -m scripts.evaluate run \
  --config configs/evals/rag-regression.yaml \
  --output data/evaluations

# 查看结果并和 baseline 比较
PYTHONPATH=backend/src .venv/bin/python -m scripts.evaluate report --run RUN_ID
PYTHONPATH=backend/src .venv/bin/python -m scripts.evaluate compare \
  --run RUN_ID --baseline BASELINE_ID
```

配置解析放在 `infrastructure/evaluation/config_loader.py`，运行前做字段白名单、数值范围、evaluator registry 和 gate metric 名称校验。API key 只能从现有 settings/environment 读取，报告中只能保存 provider、model 和 masked endpoint，不能保存密钥。

## 7. P3：HTTP API 和 SSE

### 7.1 应用服务注入

新增 `evaluation_service_factory` 和 `evaluation_query_factory`，按现有 `create_app()` 工厂模式注入：

```text
backend/src/interfaces/http/controllers/evaluation_controller.py
backend/src/application/evaluation/query_service.py
backend/src/application/evaluation/responses.py
```

在 `backend/src/bootstrap/app.py` 增加两个可选 factory，在 `backend/src/bootstrap/postgres.py` 组装生产实现，在 `backend/tests/fakes/dependencies.py` 组装内存实现。没有 factory 时不注册评测路由，保持现有轻量应用测试可启动。

### 7.2 请求和响应

`POST /api/evaluations/runs`：

```json
{
  "config": "rag-regression",
  "dataset_version": "rag-cn@2026-10-18-a1b2c3",
  "mode": "retrieval",
  "baseline_run_id": null
}
```

返回：`{"run_id":"...","status":"queued","case_count":8}`。

`GET /api/evaluations/runs/{run_id}` 返回运行快照、状态、进度、汇总指标和 gate；`GET /cases` 返回分页逐题结果；`GET /metrics` 支持 `metric_name`、`case_id` 和 `group_by=tag`；`GET /compare` 返回当前值、baseline 值、delta 和置信区间。

Controller 只负责 DTO 校验和 HTTP 状态码：dataset/config 不存在返回 404，参数冲突返回 422，run 不存在返回 404，取消已完成 run 返回 409。

### 7.3 事件

在现有 event envelope 中增加：

```text
evaluation.started
evaluation.case_started
evaluation.case_completed
evaluation.case_failed
evaluation.completed
evaluation.failed
```

payload 至少包含 `evaluation_run_id`、`case_id`、`completed_count`、`case_count`、`status`。事件只用于进度展示，最终结果以 repository 为准。

## 8. P4：Chat 和 Agent 评测

### 8.1 Chat runner

`ChatEvaluationTarget` 通过一个隔离 session 执行每个 case，保存 message/run ID 和输出快照。每个 case 使用独立 session，避免上下文串题；如果需要多轮案例，把 turns 放在 `input` 中并在执行器内部按顺序提交。

确定性 evaluator：

```text
answer.must_include_pass_rate
answer.must_not_include_pass_rate
answer.exact_match
answer.json_schema_valid
answer.citation_precision
answer.citation_recall
```

### 8.2 Agent runner

Agent target 必须使用测试 workspace、假工具和默认无网络 sandbox。每个假工具记录调用次数、参数、返回值和副作用 token。规则 evaluator 根据 `agent_runs`、`tool_calls` 和 approval event 计算：

```text
agent.task_success
agent.required_tool_recall
agent.forbidden_tool_rate
agent.argument_schema_valid_rate
agent.approval_compliance
agent.unnecessary_call_rate
agent.step_count
```

工具 stub 的结果要可按 case 配置，禁止直接连接真实 MCP、文件存储、邮件、支付或项目管理服务。

## 9. P5：LLM Judge

### 9.1 接口

新增 `infrastructure/evaluation/judge.py`，通过现有 LLM provider port 调用 Judge。domain 只看到：

```python
class JudgeEvaluator:
    async def evaluate(
        self,
        case: EvaluationCase,
        artifact: ExecutionArtifact,
    ) -> tuple[MetricValue, ...]: ...
```

Judge 输入只包含必要的 case、输出和证据，脱敏后再发送。Prompt、model、temperature、schema、attempt 和原始解析错误存入 `judge_config_json` 或 `evidence_json`；报告默认展示解析后的理由，不展示密钥或完整内部提示词。

### 9.2 解析和重试

使用结构化 JSON Schema；解析失败最多重试一次，并保存每次 attempt。两次失败时 `judge.parse_error=1`，case 状态为 `partial`，不能默认为 pass。关键集支持第二 Judge，保存 `judge_agreement`。

## 10. P5：前端实施

需要修改：

```text
frontend/src/types/index.ts
frontend/src/api/client.ts
frontend/src/store/chatStore.ts
frontend/src/components/NavRail.tsx
frontend/src/components/pages/PageView.tsx
frontend/src/components/pages/EvaluationView.tsx
```

新增 `AppView = "evaluation"`，沿用 `RetrievalView` 的 `ViewShell`、`DataTable`、`StatCard` 和分页模式。页面分为数据集、运行列表、运行详情和逐题详情四个状态，不在前端重新计算指标。前端只展示 API 返回的 score、delta、gate、evidence 和 trace 链接。

第一版只支持：选择 dataset version、启动 retrieval run、查看进度、查看指标和失败 case、查看 baseline delta。Judge 理由、Agent trace 和图表放到后续迭代。

## 11. 测试计划

### P0/P1 单元测试

```text
backend/tests/unit/test_evaluation_metrics.py
  - 二值/graded Recall、MRR、nDCG
  - 重复排名去重
  - 空相关集和 invalid case

backend/tests/unit/test_evaluation_dataset_loader.py
  - 重复 case_id、非法 grade、冲突工具约束
  - 稳定 hash、旧格式兼容

backend/tests/unit/test_evaluation_service.py
  - case 成功、超时、重试、部分失败
  - 幂等 attempt
  - run 状态转移

backend/tests/unit/test_evaluation_gates.py
  - absolute/delta、缺失值、baseline 不存在
  - 退出码映射
```

### P2 持久化和契约测试

```text
backend/tests/unit/test_evaluation_repository.py
backend/tests/unit/test_evaluation_controller.py
backend/tests/integration/test_evaluation_postgres.py
```

Repository 测试验证排序、分页、唯一约束和 JSON round-trip；Controller 测试使用 `build_test_dependencies()`，验证无 factory 时路由不出现，有 factory 时 DTO 和错误码稳定；PostgreSQL 测试验证外键、重复 attempt 和 run/case/metric 查询。

### 回归集

把现有 `backend/tests/retrieval_eval/chinese_cases.json` 转成 JSONL 新格式，至少保留 8 个 case。CI smoke 集固定使用这 8 个 case；full 集可以按环境变量指定。CI 不依赖真实 LLM Judge，使用 fake target 和确定性指标。

## 12. 配置和可观测性

新增 settings：

```text
EVALUATION_ENABLED=false
EVALUATION_DATASET_ROOT=./data/evaluations/datasets
EVALUATION_OUTPUT_ROOT=./data/evaluations/runs
EVALUATION_DEFAULT_CONCURRENCY=1
EVALUATION_CASE_TIMEOUT_SECONDS=60
EVALUATION_MAX_ATTEMPTS=1
EVALUATION_JUDGE_ENABLED=false
```

日志字段统一包含 `evaluation_run_id`、`case_id`、`attempt_id`、`evaluator`、`metric_name`。禁止把完整输入、API key、Authorization header 和未脱敏工具参数写入普通日志。运行级指标通过已有 observability 机制记录开始、完成、失败、耗时和 case 计数。

## 13. 发布和回滚

1. 先发布 P0/P1，默认 `EVALUATION_ENABLED=false`，只从 CLI 执行。
2. 连续三次 smoke run 的结果、报告和 hash 一致后启用 P2 CI gate。
3. P3 API 默认只读查询；启动 run 需要显式开启配置。
4. P4/P5 以 feature flag 开启，Judge 费用和失败率单独监控。
5. 新表或代码异常时，关闭 feature flag 不影响线上 chat/retrieval；历史 run 保留，可继续查询。

## 14. 完成定义

P2 完成必须满足：

- 同一 dataset version、config 和 target 连续运行两次，manifest hash、case 数和指标计算一致；
- 每个汇总指标都能追溯到 case result，并能通过 `retrieval_run_id` 查看候选证据；
- 单题 timeout、异常和重试有独立 attempt，run 不会静默成功；
- gate 失败时 CLI 返回 1，配置/数据集错误返回 2，依赖不可用返回 3；
- `pytest` 单元和 repository 契约测试通过，现有 retrieval、query controller 和架构边界测试不回归；
- CI smoke run 能阻止 Recall、失败率或延迟超过阈值的版本。

P5 完成再增加 Judge、Agent 和前端验收，不把它们作为 Retrieval MVP 的前置条件。

## 15. 首个迭代任务清单

建议按以下顺序开工：

1. 建立 `domain/evaluation` 的实体、端口和状态枚举。
2. 实现 JSONL loader、旧中文数据适配和稳定 manifest hash。
3. 实现 fake retrieval target 与确定性 metrics，并补单元测试。
4. 实现 `EvaluationService.run()` 和 JSON/Markdown 报告。
5. 建立 PostgreSQL ORM、repository 和内存 fake。
6. 实现 gate evaluator、baseline compare 和 CLI 退出码。
7. 把 CLI 接入现有 `evaluate_chinese_embedding.py`，保留兼容输出字段。
8. 在 CI 中加入 smoke 命令和 artifact 上传。

第 8 项和后续 HTTP、Agent、Judge、前端工作均已完成，当前版本已经能阻断检索回归，同时保留不依赖外部服务的 smoke 路径。

仓库中的 `.github/workflows/retrieval-evaluation.yml` 已按上述路径执行 P0/P1 编译、架构边界、评测单元测试和 smoke CLI，并通过 `actions/upload-artifact` 上传 `report.json`、`report.md` 和 `case-results.jsonl`。2026-10-19 验证结果：后端 `154 passed, 2 skipped`，前端 `npm run build` 通过，`git diff --check` 通过。
