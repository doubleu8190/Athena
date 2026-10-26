-- One-time cleanup for objects confirmed unused by the current application.
-- This removes data in the dropped tables/columns. The local athena database was
-- audited first: code tables and file_chunks were empty; the candidate agent
-- run values were NULL/zero; task_executions had no rows.
-- No CASCADE is used, so PostgreSQL will stop if an external dependency exists.

BEGIN;

SET LOCAL search_path TO public;

DROP INDEX IF EXISTS idx_file_chunks_content_fts;

ALTER TABLE IF EXISTS file_chunks
    DROP COLUMN IF EXISTS content_fts;

ALTER TABLE IF EXISTS agent_runs
    DROP COLUMN IF EXISTS user_id,
    DROP COLUMN IF EXISTS root_llm_turn_count,
    DROP COLUMN IF EXISTS worker_llm_turn_count,
    DROP COLUMN IF EXISTS total_worker_llm_turn_count,
    DROP COLUMN IF EXISTS tool_call_count;

ALTER TABLE IF EXISTS task_executions
    DROP COLUMN IF EXISTS checkpoint_ref;

DROP TABLE IF EXISTS code_dependencies;
DROP TABLE IF EXISTS code_symbols;

COMMIT;
