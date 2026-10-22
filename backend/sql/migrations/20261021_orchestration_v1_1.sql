-- v1.1 durable Plan version, Round, execution, and message delivery ledger.
-- Apply while the application is stopped or before deploying code that writes
-- these tables. Existing agent_events remains the user-facing replay stream.

BEGIN;

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS root_wait_round_id VARCHAR,
    ADD COLUMN IF NOT EXISTS root_wait_plan_version INTEGER,
    ADD COLUMN IF NOT EXISTS root_wait_interrupt_id VARCHAR,
    ADD COLUMN IF NOT EXISTS cancellation_epoch INTEGER NOT NULL DEFAULT 0;

ALTER TABLE agent_plans
    ADD COLUMN IF NOT EXISTS current_version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS active_round_id VARCHAR,
    ADD COLUMN IF NOT EXISTS cancellation_epoch INTEGER NOT NULL DEFAULT 0;

ALTER TABLE agent_tasks
    ADD COLUMN IF NOT EXISTS active_execution_id VARCHAR;

ALTER TABLE approvals
    ADD COLUMN IF NOT EXISTS execution_id VARCHAR,
    ADD COLUMN IF NOT EXISTS interrupt_id VARCHAR,
    ADD COLUMN IF NOT EXISTS args_hash VARCHAR,
    ADD COLUMN IF NOT EXISTS operation_key VARCHAR,
    ADD COLUMN IF NOT EXISTS decided_by VARCHAR,
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

DROP INDEX IF EXISTS one_active_run_per_session;
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_session
    ON agent_runs(session_id)
    WHERE status IN ('created', 'running', 'cancelling');

CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id VARCHAR NOT NULL REFERENCES agent_plans(plan_id),
    version INTEGER NOT NULL,
    dag_json JSONB NOT NULL,
    change_reason TEXT,
    based_on_round_id VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT fk_plan_round_version FOREIGN KEY(plan_id, plan_version)
        REFERENCES plan_versions(plan_id, version),
    CONSTRAINT uq_plan_round_number UNIQUE(plan_id, round_number),
    CONSTRAINT ck_plan_round_counts CHECK (
        expected_task_count >= 0 AND terminal_task_count >= 0
        AND terminal_task_count <= expected_task_count
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_round_active ON plan_rounds(plan_id) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS round_tasks (
    round_id VARCHAR NOT NULL REFERENCES plan_rounds(round_id),
    task_id VARCHAR NOT NULL REFERENCES agent_tasks(task_id),
    status VARCHAR NOT NULL DEFAULT 'pending',
    active_execution_id VARCHAR,
    terminal_at TIMESTAMPTZ,
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
    lease_until TIMESTAMPTZ,
    fencing_token INTEGER NOT NULL,
    cancellation_epoch INTEGER NOT NULL DEFAULT 0,
    checkpoint_ref VARCHAR,
    result_ref VARCHAR,
    error_json JSONB,
    result_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_task_execution_round_task FOREIGN KEY(round_id, task_id)
        REFERENCES round_tasks(round_id, task_id),
    CONSTRAINT uq_task_execution_attempt UNIQUE(task_id, attempt),
    CONSTRAINT ck_task_execution_attempt CHECK(attempt > 0)
);
CREATE INDEX IF NOT EXISTS idx_task_executions_lease ON task_executions(status, lease_until);

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
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_orchestration_command_idempotency UNIQUE(scope_key, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_orchestration_commands_plan ON orchestration_commands(plan_id, created_at);

CREATE TABLE IF NOT EXISTS orchestration_outbox (
    message_id VARCHAR PRIMARY KEY,
    dedupe_key VARCHAR NOT NULL UNIQUE,
    event_type VARCHAR NOT NULL,
    correlation_id VARCHAR NOT NULL,
    plan_id VARCHAR NOT NULL,
    plan_version INTEGER,
    round_id VARCHAR,
    task_id VARCHAR,
    execution_id VARCHAR,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    schema_version INTEGER NOT NULL DEFAULT 1,
    status VARCHAR NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner VARCHAR,
    lease_until TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_orchestration_outbox_ready ON orchestration_outbox(status, available_at);

CREATE TABLE IF NOT EXISTS orchestration_inbox (
    consumer_name VARCHAR NOT NULL,
    message_id VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload_hash VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'received',
    attempt INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    receipt_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    PRIMARY KEY(consumer_name, message_id)
);
CREATE INDEX IF NOT EXISTS idx_orchestration_inbox_state
    ON orchestration_inbox(consumer_name, status, created_at);

CREATE TABLE IF NOT EXISTS worker_jobs (
    job_id VARCHAR PRIMARY KEY,
    dedupe_key VARCHAR NOT NULL UNIQUE,
    job_type VARCHAR NOT NULL,
    execution_id VARCHAR REFERENCES task_executions(execution_id),
    approval_id VARCHAR REFERENCES approvals(approval_id),
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner VARCHAR,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_worker_jobs_ready ON worker_jobs(status, available_at);

CREATE TABLE IF NOT EXISTS root_resume_jobs (
    job_id VARCHAR PRIMARY KEY,
    dedupe_key VARCHAR NOT NULL UNIQUE,
    run_id VARCHAR NOT NULL REFERENCES agent_runs(run_id),
    plan_id VARCHAR NOT NULL REFERENCES agent_plans(plan_id),
    round_id VARCHAR NOT NULL REFERENCES plan_rounds(round_id),
    root_wait_generation INTEGER NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner VARCHAR,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_root_resume_jobs_ready ON root_resume_jobs(status, available_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_approvals_worker_interrupt
    ON approvals(worker_thread_id, interrupt_id)
    WHERE worker_thread_id IS NOT NULL AND interrupt_id IS NOT NULL;

COMMIT;
