-- Persist large Task outputs once and keep projections compact.

BEGIN;

CREATE TABLE IF NOT EXISTS task_execution_results (
    result_ref VARCHAR PRIMARY KEY,
    execution_id VARCHAR NOT NULL UNIQUE
        REFERENCES task_executions(execution_id) ON DELETE CASCADE,
    result_json JSONB NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    content_bytes INTEGER NOT NULL CHECK(content_bytes > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;
