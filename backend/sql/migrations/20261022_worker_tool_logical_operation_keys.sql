ALTER TABLE worker_tool_operations
    ADD COLUMN IF NOT EXISTS logical_operation_key VARCHAR;

UPDATE worker_tool_operations
SET logical_operation_key = CASE
    WHEN tool_call_id ~ '^worker:.*:[0-9]+:[0-9]+:[0-9]+$' THEN
        'athena:' || run_id || ':' || task_id || ':logical:' ||
        (regexp_match(
            tool_call_id,
            '^worker:.*:[0-9]+:([0-9]+):([0-9]+)$'
        ))[1] || ':' ||
        (regexp_match(
            tool_call_id,
            '^worker:.*:[0-9]+:([0-9]+):([0-9]+)$'
        ))[2]
    ELSE 'legacy:' || operation_key
END
WHERE logical_operation_key IS NULL;

ALTER TABLE worker_tool_operations
    ALTER COLUMN logical_operation_key SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_worker_tool_operations_logical
    ON worker_tool_operations(logical_operation_key);
