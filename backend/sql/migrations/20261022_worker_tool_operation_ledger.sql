CREATE TABLE IF NOT EXISTS worker_tool_operations (
    operation_key VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL REFERENCES agent_runs(run_id),
    plan_id VARCHAR NOT NULL REFERENCES agent_plans(plan_id),
    task_id VARCHAR NOT NULL REFERENCES agent_tasks(task_id),
    execution_id VARCHAR NOT NULL REFERENCES task_executions(execution_id),
    tool_name VARCHAR NOT NULL,
    tool_call_id VARCHAR NOT NULL,
    args_hash VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'executing',
    result_json JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_worker_tool_operations_execution
    ON worker_tool_operations(execution_id);
CREATE INDEX IF NOT EXISTS idx_worker_tool_operations_incomplete
    ON worker_tool_operations(status, created_at)
    WHERE status = 'executing';
