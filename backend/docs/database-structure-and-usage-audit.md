# 数据库表结构与使用情况审查

> 审查范围：当前仓库中的 PostgreSQL SQLAlchemy ORM 模型，以及 `backend/src`、`frontend/src` 中对列名的直接引用。此文档是静态代码审查，不代表对某个运行中数据库的访问统计。

## 结论

- ORM 当前定义 **44 张表、533 个映射字段**。
- 旧版 SQL 重建脚本已移除；当前 schema 由 PostgreSQL ORM 模型定义。
- 直接引用扫描发现下列字段目前没有应用/前端源代码引用，详见“疑似未使用字段”。它们仍可能被 ORM 整行查询、序列化、迁移或外部 SQL 使用，因此不能仅凭扫描结果删除。
- 项目虚拟环境可导入 SQLAlchemy 2.1.5，但当前没有 `psql` 客户端，也没有连接到运行中的数据库；因此无法核验实际部署 schema、数据量、索引命中率或 `pg_stat` 访问记录。

## 疑似未使用字段

| 表 | 字段 | 静态证据 | 建议核实方向 |
|---|---|---|---|
| `agent_runs` | `user_id` | `backend/src` 与 `frontend/src` 无直接引用；测试和设计文档有提及 | 产品为单用户时确认是否保留预留；核对线上写入与外部查询 |
| `agent_runs` | `root_llm_turn_count`, `worker_llm_turn_count`, `total_worker_llm_turn_count`, `tool_call_count` | 4 个字段未在应用/前端源码直接引用 | 确认是否应由运行计量/接口读出；检查部署版本和统计消费方 |
| `memories` | `content_fts` | 计算列在源码中无直接查询引用；记忆检索是否改走向量/其他 FTS 需结合 SQL 路径确认 | 检查实际检索 SQL、GIN 索引使用情况 |
| `file_chunks` | `content_fts` | 计算列在源码中无直接查询引用 | 检查知识检索的原生/全文检索路径及 GIN 索引命中情况 |
| `code_symbols` | `qualified_name`, `start_line`, `end_line` | 字段名在应用/前端源码中无直接引用 | 确认代码符号索引查询是否尚未接入或由外部流程使用 |
| `eval_runs` | `model_snapshot_json`, `judge_config_json` | 应用/前端源码无直接引用；字段名只见于设计/实现文档 | 检查评估运行创建与报告接口是否遗漏持久化/读取 |
| `task_executions` | `checkpoint_ref` | 应用/前端源码无直接引用 | 确认 checkpoint 当前通过别的存储/引用字段管理，或此列已过时 |

> “未直接引用”不等于数据库层面未使用。删除前应检查生产 schema、写入数据、API 输出、离线任务/报表、手写 SQL，以及迁移和回滚要求。

## 表字段目录

字段仅列 ORM 映射列名；主键、外键与索引以模型定义为准。

### `evaluation_models.py`

- **`eval_case_attempts`** (17): `attempt_id, run_id, case_id, version_id, attempt_number, status, input_snapshot_json, output_text, trace_json, retrieval_run_id, agent_run_id, duration_ms, input_tokens, output_tokens, estimated_cost, error_message, created_at`
- **`eval_cases`** (6): `case_id, version_id, task_type, case_json, tags_json, created_at`
- **`eval_dataset_versions`** (9): `version_id, dataset_id, version, manifest_sha256, status, case_count, schema_version, manifest_json, created_at`
- **`eval_datasets`** (7): `dataset_id, name, description, task_type, owner, created_at, updated_at`
- **`eval_gates`** (9): `gate_id, run_id, metric, status, rule_json, actual, baseline, reason, created_at`
- **`eval_metric_values`** (11): `metric_id, run_id, case_id, version_id, attempt_id, metric_name, value_numeric, value_json, evaluator_version, evidence_json, created_at`
- **`eval_runs`** (17): `run_id, version_id, mode, status, name, git_sha, config_json, model_snapshot_json, judge_config_json, baseline_run_id, case_count, completed_count, failed_count, started_at, completed_at, created_at, error_message`

### `file_models.py`

- **`adapter_registry`** (7): `name, version, mime_types_json, extensions_json, capabilities_json, enabled, updated_at`
- **`attachments`** (20): `id, logical_document_id, document_version, session_id, knowledge_base_id, message_id, filename, mime_type, size_bytes, sha256, storage_key, adapter_name, adapter_version, status, capabilities_json, parsed_metadata_json, error_message, created_at, updated_at, deleted_time`
- **`file_artifacts`** (8): `id, attachment_id, kind, cache_key, content, storage_key, metadata_json, created_at`
- **`file_chunks`** (11): `id, attachment_id, ordinal, content, embedding, content_fts, token_count, locator_json, metadata_json, native_score, deleted_time`
- **`knowledge_bases`** (6): `id, name, description, created_at, updated_at, deleted_time`

### `orchestration_models.py`

- **`orchestration_commands`** (11): `message_id, scope_key, idempotency_key, payload_hash, command_type, plan_id, plan_version, round_id, status, result_json, created_at`
- **`orchestration_inbox`** (11): `consumer_name, message_id, event_type, payload_json, payload_hash, status, attempt, last_error, receipt_json, created_at, processed_at`
- **`orchestration_outbox`** (18): `message_id, dedupe_key, event_type, correlation_id, plan_id, plan_version, round_id, task_id, execution_id, payload_json, schema_version, status, attempt, available_at, lease_owner, lease_until, published_at, occurred_at`
- **`plan_rounds`** (9): `round_id, plan_id, plan_version, round_number, status, expected_task_count, terminal_task_count, created_at, completed_at`
- **`plan_versions`** (6): `plan_id, version, dag_json, change_reason, based_on_round_id, created_at`
- **`root_resume_jobs`** (13): `job_id, dedupe_key, run_id, plan_id, round_id, root_wait_generation, status, attempt, available_at, lease_owner, lease_until, created_at, updated_at`
- **`round_tasks`** (5): `round_id, task_id, status, active_execution_id, terminal_at`
- **`task_execution_results`** (6): `result_ref, execution_id, result_json, content_sha256, content_bytes, created_at`
- **`task_executions`** (16): `execution_id, round_id, task_id, attempt, worker_thread_id, status, lease_owner, lease_until, fencing_token, cancellation_epoch, checkpoint_ref, result_ref, error_json, result_json, created_at, updated_at`
- **`worker_jobs`** (13): `job_id, dedupe_key, job_type, execution_id, approval_id, payload_json, status, attempt, available_at, lease_owner, lease_until, created_at, updated_at`

### `runtime_models.py`

- **`agent_commands`** (13): `command_id, session_id, run_id, command_type, schema_version, idempotency_key, payload_json, status, result_json, error_json, queued_at, started_at, completed_at`
- **`agent_events`** (14): `session_id, session_seq, run_id, message_id, attachment_id, event_type, durability, stream_id, stream_type, is_complete, parent_run_id, transition_id, payload_json, occurred_at`
- **`agent_plans`** (11): `plan_id, run_id, session_id, status, user_goal, max_parallelism, current_version, active_round_id, cancellation_epoch, created_at, updated_at`
- **`agent_runs`** (22): `run_id, session_id, user_id, status, request_json, response_json, error_json, root_thread_id, root_llm_turn_count, worker_llm_turn_count, total_worker_llm_turn_count, tool_call_count, root_wait_generation, root_wait_checkpoint_id, root_wait_armed_at, root_wait_round_id, root_wait_plan_version, root_wait_interrupt_id, cancellation_epoch, cancellation_external_effect_status, created_at, updated_at`
- **`agent_tasks`** (19): `task_id, plan_id, run_id, title, objective, input_json, expected_output_json, allowed_tools_json, worker_type, timeout_seconds, status, output_json, error_json, execution_generation, worker_thread_id, active_execution_id, llm_turn_count, created_at, updated_at`
- **`approvals`** (21): `approval_id, session_id, run_id, approval_batch_id, plan_id, task_id, execution_id, worker_thread_id, interrupt_id, tool_call_id, tool_name, arguments_json, risk_level, args_hash, operation_key, status, decision, decided_by, created_at, expires_at, decided_at`
- **`code_dependencies`** (6): `id, attachment_id, source, target, kind, metadata_json`
- **`code_symbols`** (10): `id, attachment_id, path, language, name, qualified_name, kind, start_line, end_line, signature`
- **`embedding_metadata`** (3): `id, signature, dimension`
- **`knowledge_document_jobs`** (9): `job_id, attachment_id, status, attempt, available_at, result_json, error_json, created_at, updated_at`
- **`memories`** (22): `id, logical_memory_id, session_id, content, embedding, content_fts, metadata_json, expires_at, created_at, last_accessed_at, access_count, memory_type, category, confidence, source_kind, status, source_turn_id, last_observed_at, validity_status, valid_until, revision, deleted_time`
- **`memory_processing_jobs`** (11): `job_id, turn_id, session_id, user_text, assistant_text, status, attempt, available_at, error_json, created_at, updated_at`
- **`retrieval_candidates`** (22): `candidate_id, run_id, provider, stage, source_type, source_id, logical_source_id, revision_id, native_rank, native_score, fused_rank, fused_score, rerank_rank, rerank_score, filter_reason, selected_for_result, injected_into_context, content_preview, source_title, locator_json, metadata_json, created_at`
- **`retrieval_runs`** (16): `run_id, query, scope, status, config_json, index_generation, session_id, agent_run_id, message_id, candidate_count, selected_count, injected_count, duration_ms, error_message, created_at, completed_at`
- **`steps`** (15): `id, session_id, run_id, step_number, step_type, parent_step_id, parent_run_id, status, started_at, completed_at, duration_ms, llm_input_tokens, llm_output_tokens, error_message, deleted_time`
- **`tool_calls`** (16): `id, session_id, step_id, run_id, attempt_number, approval_id, tool_name, arguments_json, raw_output, status, started_at, completed_at, duration_ms, error_message, error_stack, deleted_time`

### `session_models.py`

- **`messages`** (12): `id, session_id, role, content, tool_calls_json, tool_call_id, run_id, tool_call_record_id, tool_name, message_type, timestamp, deleted_time`
- **`sessions`** (10): `id, title, status, run_id, created_at, updated_at, compression_summary, last_compressed_message_id, last_summarized_message_id, deleted_time`

### `tool_models.py`

- **`mcp_servers`** (4): `name, config_json, created_at, deleted_time`
- **`tools`** (11): `tool_name, execution_mode, server_name, remote_name, description, parameters_schema_json, risk_level, require_approval, enabled, created_at, updated_at`

### `tool_operation_models.py`

- **`worker_tool_operations`** (13): `operation_key, logical_operation_key, run_id, plan_id, task_id, execution_id, tool_name, tool_call_id, args_hash, status, result_json, created_at, completed_at`

### `worker_execution_message_models.py`

- **`worker_execution_messages`** (17): `message_id, session_id, run_id, plan_id, task_id, execution_id, execution_generation, invocation_id, sequence_no, entry_type, role, content_json, tool_call_id, tool_name, operation_key, result_ref, created_at`

## 表关系图

图中实线表示 ORM 显式声明的外键（含复合外键）；未画出只有同名字段但没有数据库 FK 约束的逻辑关联。

```mermaid
erDiagram
    sessions ||--o{ messages : session_id
    sessions ||--o{ worker_execution_messages : session_id
    knowledge_bases o|--o{ attachments : knowledge_base_id
    attachments ||--o{ file_chunks : attachment_id
    attachments ||--o{ file_artifacts : attachment_id
    attachments ||--o| knowledge_document_jobs : attachment_id
    attachments ||--o{ code_symbols : attachment_id
    attachments ||--o{ code_dependencies : attachment_id
    steps o|--o{ tool_calls : step_id
    agent_plans ||--o{ agent_tasks : plan_id
    agent_plans ||--o{ plan_versions : plan_id
    plan_versions ||--o{ plan_rounds : plan_id_version
    plan_rounds ||--o{ round_tasks : round_id
    agent_tasks ||--o{ round_tasks : task_id
    round_tasks ||--o{ task_executions : round_id_task_id
    task_executions ||--o| task_execution_results : execution_id
    task_executions o|--o{ worker_jobs : execution_id
    approvals o|--o{ worker_jobs : approval_id
    agent_runs ||--o{ root_resume_jobs : run_id
    agent_plans ||--o{ root_resume_jobs : plan_id
    plan_rounds ||--o{ root_resume_jobs : round_id
    agent_runs ||--o{ worker_tool_operations : run_id
    agent_plans ||--o{ worker_tool_operations : plan_id
    agent_tasks ||--o{ worker_tool_operations : task_id
    task_executions ||--o{ worker_tool_operations : execution_id
    agent_runs ||--o{ worker_execution_messages : run_id
    agent_plans ||--o{ worker_execution_messages : plan_id
    agent_tasks ||--o{ worker_execution_messages : task_id
    task_executions ||--o{ worker_execution_messages : execution_id
    eval_datasets ||--o{ eval_dataset_versions : dataset_id
    eval_dataset_versions ||--o{ eval_cases : version_id
    eval_runs ||--o{ eval_case_attempts : run_id
    eval_dataset_versions ||--o{ eval_case_attempts : version_id
    eval_cases ||--o{ eval_case_attempts : version_id_case_id
    eval_runs ||--o{ eval_metric_values : run_id
    eval_runs ||--o{ eval_gates : run_id
    retrieval_runs ||--o{ retrieval_candidates : run_id
```

### 重要的逻辑关联（当前未声明外键）

- `agent_runs.session_id → sessions.id`、`agent_plans.run_id → agent_runs.run_id`、`agent_plans.session_id → sessions.id`：ORM 字段存在，但模型没有 `ForeignKey` 约束。
- `agent_events` 的 `session_id/run_id/message_id/attachment_id`、`approvals` 的业务 ID 字段、`eval_runs.version_id → eval_dataset_versions.version_id` 等均依赖应用层关联。
- `messages.tool_call_record_id`、`messages.run_id` 与 `tool_calls` / `agent_runs` 之间也没有 FK。

## 使用边界

- 本次“未使用”判定采用直接源码列名搜索，不等于动态查询、反射访问、数据库函数、BI/脚本或外部调用未使用。
- 要做可删除性结论，建议在目标 PostgreSQL 上按环境分别核对 `information_schema` / `pg_constraint` / `pg_indexes`，并观察 `pg_stat_user_tables`、`pg_stat_user_indexes` 及应用 SQL 日志；统计视图受 reset 时间和权限影响。
- 目前没有执行数据库变更，也没有修改现有模型或迁移。
