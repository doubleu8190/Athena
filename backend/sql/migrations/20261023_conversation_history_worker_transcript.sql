CREATE INDEX IF NOT EXISTS idx_messages_session_timestamp_id
    ON messages(session_id, timestamp, id);

CREATE TABLE IF NOT EXISTS worker_execution_messages (
    message_id VARCHAR PRIMARY KEY,
    session_id VARCHAR NOT NULL REFERENCES sessions(id),
    run_id VARCHAR NOT NULL REFERENCES agent_runs(run_id),
    plan_id VARCHAR NOT NULL REFERENCES agent_plans(plan_id),
    task_id VARCHAR NOT NULL REFERENCES agent_tasks(task_id),
    execution_id VARCHAR NOT NULL REFERENCES task_executions(execution_id),
    execution_generation INTEGER NOT NULL CHECK (execution_generation > 0),
    invocation_id VARCHAR NOT NULL,
    sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
    entry_type VARCHAR NOT NULL CHECK (entry_type IN ('llm_message', 'tool_result')),
    role VARCHAR,
    content_json JSONB NOT NULL,
    tool_call_id VARCHAR,
    tool_name VARCHAR,
    operation_key VARCHAR,
    result_ref VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_worker_execution_messages_sequence
        UNIQUE (task_id, execution_generation, sequence_no),
    CONSTRAINT ck_worker_execution_messages_role
        CHECK (
            (entry_type = 'llm_message' AND role IS NOT NULL
                AND role IN ('system', 'user', 'assistant'))
            OR (entry_type = 'tool_result' AND role IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_worker_execution_messages_run
    ON worker_execution_messages(run_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_worker_execution_messages_tool_call
    ON worker_execution_messages(tool_call_id)
    WHERE tool_call_id IS NOT NULL;
