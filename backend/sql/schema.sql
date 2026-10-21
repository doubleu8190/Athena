-- =============================================================
-- Athena PostgreSQL reference schema. Application startup recreates the
-- development database from SQLAlchemy metadata.
-- =============================================================
-- Athena 数据库重建脚本（历史 SQLite 布局）
-- 重新生成方法：
--   python3 -c "from sqlalchemy.schema import CreateTable,CreateIndex; \
--     from sqlalchemy.dialects import sqlite; from athena.db.models import Base,MEMORY_FTS_DDL; \
--     d=sqlite.dialect(); \
--     [print(str(CreateTable(t).compile(dialect=d)).strip()+';') or [print(str(CreateIndex(i).compile(dialect=d)).strip()+';') for i in t.indexes] for t in Base.metadata.sorted_tables]; \
--     print(MEMORY_FTS_DDL.strip()); print('PRAGMA user_version = 1;')" > athena/db/schema.sql
--
-- 用法：
--   sqlite3 data/athena.db < backend/sql/schema.sql
--
-- 记忆表（memories/memory_relations/memory_processing_jobs/memory_fts）由
-- 应用按 sqlite_runtime_db_path、sqlite_knowledge_db_path、
-- sqlite_telemetry_db_path 和 sqlite_memory_db_path 分别初始化独立数据库；
-- 本文件仅描述 ORM 统一模型，实际建表时会按数据库职责筛选表。
-- =============================================================

CREATE TABLE sessions (
	id VARCHAR NOT NULL, 
	title VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	run_id VARCHAR, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	compression_summary TEXT, 
	last_compressed_message_id VARCHAR, 
	last_summarized_message_id VARCHAR, 
	deleted_time VARCHAR, 
	PRIMARY KEY (id)
);

CREATE TABLE messages (
	id VARCHAR NOT NULL, 
	session_id VARCHAR NOT NULL, 
	role VARCHAR NOT NULL, 
	content TEXT NOT NULL, 
	tool_calls_json TEXT NOT NULL, 
	tool_call_id VARCHAR, 
	run_id VARCHAR, 
	tool_call_record_id VARCHAR, 
	tool_name VARCHAR, 
	message_type VARCHAR,
	timestamp VARCHAR NOT NULL, 
	deleted_time VARCHAR, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sessions (id)
);
CREATE INDEX idx_messages_session ON messages (session_id);
CREATE INDEX idx_messages_timestamp ON messages (timestamp);

CREATE TABLE steps (
	id VARCHAR NOT NULL,
	session_id VARCHAR NOT NULL,
	run_id VARCHAR NOT NULL,
	step_number INTEGER NOT NULL,
	step_type VARCHAR NOT NULL,
	parent_step_id VARCHAR,
	parent_run_id VARCHAR,
	status VARCHAR NOT NULL,
	started_at VARCHAR NOT NULL,
	completed_at VARCHAR,
	duration_ms FLOAT NOT NULL,
	llm_input_tokens INTEGER NOT NULL,
	llm_output_tokens INTEGER NOT NULL,
	error_message TEXT,
	deleted_time VARCHAR,
	PRIMARY KEY (id),
	FOREIGN KEY(session_id) REFERENCES sessions (id)
);
CREATE INDEX idx_steps_parent ON steps (parent_step_id);
CREATE INDEX idx_steps_run ON steps (run_id);
CREATE INDEX idx_steps_session ON steps (session_id);
CREATE INDEX idx_steps_number ON steps (step_number);

CREATE TABLE tool_calls (
	id VARCHAR NOT NULL,
	session_id VARCHAR NOT NULL,
	step_id VARCHAR,
	tool_name VARCHAR NOT NULL, 
	arguments_json TEXT NOT NULL, 
	raw_output TEXT, 
	status VARCHAR NOT NULL, 
	started_at VARCHAR NOT NULL, 
	completed_at VARCHAR, 
	duration_ms FLOAT NOT NULL, 
	error_message TEXT, 
	error_stack TEXT, 
	deleted_time VARCHAR, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sessions (id),
	FOREIGN KEY(step_id) REFERENCES steps (id)
);
CREATE INDEX idx_tool_calls_status ON tool_calls (status);
CREATE INDEX idx_tool_calls_session ON tool_calls (session_id);
CREATE INDEX idx_tool_calls_step ON tool_calls (step_id);

-- Target Root/Worker runtime ledger. PostgreSQL is the business source of truth.
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id VARCHAR PRIMARY KEY,
    session_id VARCHAR NOT NULL REFERENCES sessions(id),
    user_id VARCHAR,
    status VARCHAR NOT NULL DEFAULT 'created',
    request_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT,
    error_json TEXT,
    root_thread_id VARCHAR NOT NULL,
    root_llm_turn_count INTEGER NOT NULL DEFAULT 0,
    worker_llm_turn_count INTEGER NOT NULL DEFAULT 0,
    total_worker_llm_turn_count INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    root_wait_generation INTEGER NOT NULL DEFAULT 0,
    root_wait_checkpoint_id VARCHAR,
    root_wait_armed_at VARCHAR,
    root_wait_round_id VARCHAR,
    root_wait_plan_version INTEGER,
    root_wait_interrupt_id VARCHAR,
    cancellation_epoch INTEGER NOT NULL DEFAULT 0,
    created_at VARCHAR NOT NULL,
    updated_at VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_session_status ON agent_runs(session_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_session
ON agent_runs(session_id) WHERE status IN ('created', 'running', 'cancelling');

CREATE TABLE IF NOT EXISTS agent_plans (
    plan_id VARCHAR PRIMARY KEY,
    session_id VARCHAR NOT NULL REFERENCES sessions(id),
    run_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'created',
    user_goal TEXT NOT NULL,
    max_parallelism INTEGER NOT NULL DEFAULT 1,
    plan_json TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    active_round_id VARCHAR,
    cancellation_epoch INTEGER NOT NULL DEFAULT 0,
    created_at VARCHAR NOT NULL,
    updated_at VARCHAR NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_plan_per_run ON agent_plans(run_id);

CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id VARCHAR PRIMARY KEY,
    plan_id VARCHAR NOT NULL REFERENCES agent_plans(plan_id),
    run_id VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    objective TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    expected_output_json TEXT NOT NULL DEFAULT '{}',
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    worker_type VARCHAR NOT NULL DEFAULT 'general',
    status VARCHAR NOT NULL DEFAULT 'pending',
    output_json TEXT,
    error_json TEXT,
    execution_generation INTEGER NOT NULL DEFAULT 0,
    worker_thread_id VARCHAR,
    active_execution_id VARCHAR,
    llm_turn_count INTEGER NOT NULL DEFAULT 0,
    created_at VARCHAR NOT NULL,
    updated_at VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_plan_status ON agent_tasks(plan_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_run_status ON agent_tasks(run_id, status);

CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id VARCHAR NOT NULL REFERENCES agent_plans(plan_id),
    version INTEGER NOT NULL,
    dag_json JSONB NOT NULL,
    change_reason TEXT,
    based_on_round_id VARCHAR,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY(plan_id, version)
);

CREATE TABLE IF NOT EXISTS plan_rounds (
    round_id VARCHAR PRIMARY KEY,
    plan_id VARCHAR NOT NULL,
    plan_version INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'running',
    expected_task_count INTEGER NOT NULL DEFAULT 0,
    terminal_task_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    FOREIGN KEY(plan_id, plan_version) REFERENCES plan_versions(plan_id, version),
    UNIQUE(plan_id, round_number),
    CHECK(expected_task_count >= 0 AND terminal_task_count >= 0 AND terminal_task_count <= expected_task_count)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_round_active ON plan_rounds(plan_id) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS round_tasks (
    round_id VARCHAR NOT NULL REFERENCES plan_rounds(round_id),
    task_id VARCHAR NOT NULL REFERENCES agent_tasks(task_id),
    status VARCHAR NOT NULL DEFAULT 'pending',
    active_execution_id VARCHAR,
    terminal_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY(round_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_round_tasks_round_state ON round_tasks(round_id, status);

CREATE TABLE IF NOT EXISTS task_executions (
    execution_id VARCHAR PRIMARY KEY,
    round_id VARCHAR NOT NULL,
    task_id VARCHAR NOT NULL,
    attempt INTEGER NOT NULL,
    worker_thread_id VARCHAR NOT NULL UNIQUE,
    status VARCHAR NOT NULL DEFAULT 'queued',
    lease_owner VARCHAR,
    lease_until TIMESTAMP WITH TIME ZONE,
    fencing_token INTEGER NOT NULL,
    cancellation_epoch INTEGER NOT NULL DEFAULT 0,
    checkpoint_ref VARCHAR,
    result_ref VARCHAR,
    error_json JSONB,
    result_json JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    FOREIGN KEY(round_id, task_id) REFERENCES round_tasks(round_id, task_id),
    UNIQUE(task_id, attempt),
    CHECK(attempt > 0)
);
CREATE INDEX IF NOT EXISTS idx_task_executions_lease ON task_executions(status, lease_until);

CREATE TABLE IF NOT EXISTS task_execution_results (
    result_ref VARCHAR PRIMARY KEY,
    execution_id VARCHAR NOT NULL UNIQUE
        REFERENCES task_executions(execution_id) ON DELETE CASCADE,
    result_json JSONB NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    content_bytes INTEGER NOT NULL CHECK(content_bytes > 0),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE TABLE IF NOT EXISTS orchestration_commands (
    message_id VARCHAR PRIMARY KEY,
    scope_key VARCHAR NOT NULL,
    idempotency_key VARCHAR NOT NULL,
    payload_hash VARCHAR NOT NULL,
    command_type VARCHAR NOT NULL,
    plan_id VARCHAR NOT NULL,
    plan_version INTEGER,
    round_id VARCHAR,
    status VARCHAR NOT NULL DEFAULT 'accepted',
    result_json JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    UNIQUE(scope_key, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_orchestration_commands_plan ON orchestration_commands(plan_id, created_at);

CREATE TABLE IF NOT EXISTS orchestration_outbox (
    message_id VARCHAR PRIMARY KEY,
    dedupe_key VARCHAR NOT NULL UNIQUE,
    event_type VARCHAR NOT NULL,
    correlation_id VARCHAR NOT NULL,
    plan_id VARCHAR,
    plan_version INTEGER,
    round_id VARCHAR,
    task_id VARCHAR,
    execution_id VARCHAR,
    payload_json JSONB NOT NULL DEFAULT '{}',
    schema_version INTEGER NOT NULL DEFAULT 1,
    status VARCHAR NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMP WITH TIME ZONE NOT NULL,
    lease_owner VARCHAR,
    lease_until TIMESTAMP WITH TIME ZONE,
    published_at TIMESTAMP WITH TIME ZONE,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orchestration_outbox_ready ON orchestration_outbox(status, available_at);

CREATE TABLE IF NOT EXISTS orchestration_inbox (
    consumer_name VARCHAR NOT NULL,
    message_id VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}',
    payload_hash VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'received',
    attempt INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    receipt_json JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY(consumer_name, message_id)
);
CREATE INDEX IF NOT EXISTS idx_orchestration_inbox_state ON orchestration_inbox(consumer_name, status, created_at);

CREATE TABLE IF NOT EXISTS root_resume_jobs (
    job_id VARCHAR PRIMARY KEY,
    dedupe_key VARCHAR NOT NULL UNIQUE,
    run_id VARCHAR NOT NULL REFERENCES agent_runs(run_id),
    plan_id VARCHAR NOT NULL REFERENCES agent_plans(plan_id),
    round_id VARCHAR NOT NULL REFERENCES plan_rounds(round_id),
    root_wait_generation INTEGER NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMP WITH TIME ZONE NOT NULL,
    lease_owner VARCHAR,
    lease_until TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_root_resume_jobs_ready ON root_resume_jobs(status, available_at);

CREATE TABLE IF NOT EXISTS agent_commands (
    command_id VARCHAR PRIMARY KEY,
    session_id VARCHAR NOT NULL REFERENCES sessions(id),
    run_id VARCHAR,
    command_type VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    idempotency_key VARCHAR NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status VARCHAR NOT NULL DEFAULT 'queued',
    result_json TEXT,
    error_json TEXT,
    queued_at VARCHAR NOT NULL,
    started_at VARCHAR,
    completed_at VARCHAR,
    UNIQUE(command_type, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_commands_status_queued ON agent_commands(status, queued_at);

CREATE TABLE IF NOT EXISTS agent_events (
    session_id VARCHAR NOT NULL REFERENCES sessions(id),
    session_seq INTEGER NOT NULL,
    run_id VARCHAR,
    message_id VARCHAR,
    attachment_id VARCHAR,
    event_type VARCHAR NOT NULL,
    durability VARCHAR NOT NULL,
    stream_id VARCHAR,
    stream_type VARCHAR,
    is_complete INTEGER NOT NULL DEFAULT 0,
    parent_run_id VARCHAR,
    transition_id VARCHAR,
    payload_json TEXT NOT NULL DEFAULT '{}',
    occurred_at VARCHAR NOT NULL,
    PRIMARY KEY(session_id, session_seq)
);
CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_session_run_seq ON agent_events(session_id, run_id, session_seq);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_events_session_transition_id
    ON agent_events(session_id, transition_id) WHERE transition_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS stream_snapshots (
    stream_id VARCHAR PRIMARY KEY,
    session_id VARCHAR NOT NULL REFERENCES sessions(id),
    run_id VARCHAR,
    stream_type VARCHAR NOT NULL DEFAULT 'answer',
    version INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL DEFAULT '',
    content_byte_length INTEGER NOT NULL DEFAULT 0,
    status VARCHAR NOT NULL DEFAULT 'streaming',
    updated_at VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    run_id TEXT NOT NULL,
    approval_batch_id TEXT,
    plan_id TEXT,
    task_id TEXT,
    worker_thread_id TEXT,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    risk_level TEXT NOT NULL DEFAULT 'low',
    status TEXT NOT NULL DEFAULT 'pending',
    decision TEXT,
    execution_id TEXT,
    interrupt_id TEXT,
    args_hash TEXT,
    operation_key TEXT,
    decided_by TEXT,
    expires_at TIMESTAMP WITH TIME ZONE,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_session_status ON approvals(session_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_approvals_worker_interrupt
ON approvals(worker_thread_id, interrupt_id)
WHERE worker_thread_id IS NOT NULL AND interrupt_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS worker_jobs (
    job_id VARCHAR PRIMARY KEY,
    dedupe_key VARCHAR NOT NULL UNIQUE,
    job_type VARCHAR NOT NULL,
    execution_id VARCHAR REFERENCES task_executions(execution_id),
    approval_id VARCHAR REFERENCES approvals(approval_id),
    payload_json JSONB NOT NULL DEFAULT '{}',
    status VARCHAR NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMP WITH TIME ZONE NOT NULL,
    lease_owner VARCHAR,
    lease_until TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_worker_jobs_ready ON worker_jobs(status, available_at);
CREATE INDEX IF NOT EXISTS idx_worker_jobs_lease ON worker_jobs(status, lease_until);

-- 知识库文档的持久解析/索引队列。运行态任务在应用启动时恢复为 retry；
-- 附件删除时会转为 cancelled，避免 worker 在删除后重新写入索引。
CREATE TABLE IF NOT EXISTS knowledge_document_jobs (
    job_id VARCHAR NOT NULL PRIMARY KEY,
    attachment_id VARCHAR NOT NULL UNIQUE REFERENCES attachments(id),
    status VARCHAR NOT NULL DEFAULT 'queued',
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at VARCHAR NOT NULL,
    result_json TEXT,
    error_json TEXT,
    created_at VARCHAR NOT NULL,
    updated_at VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_document_jobs_queue
ON knowledge_document_jobs(status, available_at);
