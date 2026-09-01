-- =============================================================
-- Athena 数据库重建脚本（由 athena/db/models.py 生成，勿手改）
-- 重新生成方法：
--   python3 -c "from sqlalchemy.schema import CreateTable,CreateIndex; \
--     from sqlalchemy.dialects import sqlite; from athena.db.models import Base,MEMORY_FTS_DDL; \
--     d=sqlite.dialect(); \
--     [print(str(CreateTable(t).compile(dialect=d)).strip()+';') or [print(str(CreateIndex(i).compile(dialect=d)).strip()+';') for i in t.indexes] for t in Base.metadata.sorted_tables]; \
--     print(MEMORY_FTS_DDL.strip()); print('PRAGMA user_version = 1;')" > athena/db/schema.sql
--
-- 用法：
--   sqlite3 data/athena.db < athena/db/schema.sql
-- =============================================================

PRAGMA foreign_keys = ON;

CREATE TABLE memories (
	id VARCHAR NOT NULL, 
	session_id VARCHAR NOT NULL, 
	content TEXT NOT NULL, 
	metadata_json TEXT NOT NULL, 
	pinned INTEGER NOT NULL, 
	expires_at VARCHAR, 
	created_at VARCHAR NOT NULL, 
	last_accessed VARCHAR, 
	access_count INTEGER NOT NULL, 
	type VARCHAR, 
	category VARCHAR, 
	confidence FLOAT, 
	source VARCHAR, 
	deleted_time VARCHAR, 
	PRIMARY KEY (id)
);
CREATE INDEX idx_memories_session ON memories (session_id);

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

CREATE TABLE approval_logs (
	id VARCHAR NOT NULL, 
	session_id VARCHAR NOT NULL, 
	tool_call_id VARCHAR NOT NULL, 
	tool_name VARCHAR NOT NULL, 
	arguments_json TEXT NOT NULL, 
	risk_level VARCHAR NOT NULL, 
	decision VARCHAR NOT NULL, 
	decision_time_ms FLOAT NOT NULL, 
	timestamp VARCHAR NOT NULL, 
	deleted_time VARCHAR, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sessions (id)
);
CREATE INDEX idx_approval_logs_session ON approval_logs (session_id);

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
	type VARCHAR, 
	timestamp VARCHAR NOT NULL, 
	deleted_time VARCHAR, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sessions (id)
);
CREATE INDEX idx_messages_session ON messages (session_id);
CREATE INDEX idx_messages_timestamp ON messages (timestamp);

CREATE TABLE tool_call (
	id VARCHAR NOT NULL, 
	session_id VARCHAR NOT NULL, 
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
	FOREIGN KEY(session_id) REFERENCES sessions (id)
);
CREATE INDEX idx_tool_call_status ON tool_call (status);
CREATE INDEX idx_tool_call_session ON tool_call (session_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    memory_id UNINDEXED,
    tokenize='unicode61'
);

PRAGMA user_version = 1;

-- LangGraph/SSE migration tables.  Runtime and Gateway share these tables;
-- Command IDs are globally unique, while a Run may have many Commands.
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id VARCHAR PRIMARY KEY,
    session_id VARCHAR NOT NULL REFERENCES sessions(id),
    created_by_command_id VARCHAR,
    status VARCHAR NOT NULL DEFAULT 'queued',
    pause_requested INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at VARCHAR NOT NULL,
    updated_at VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_session_status ON agent_runs(session_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_active_session ON agent_runs(session_id)
WHERE status IN ('queued','running','cancel_requested','waiting_approval','waiting_files');

CREATE TABLE IF NOT EXISTS agent_commands (
    command_id VARCHAR PRIMARY KEY,
    session_id VARCHAR NOT NULL REFERENCES sessions(id),
    run_id VARCHAR,
    command_type VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL DEFAULT '{}',
    payload_hash VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at VARCHAR NOT NULL,
    result_json TEXT,
    error_json TEXT,
    issued_at VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_commands_status_available ON agent_commands(status, available_at);

CREATE TABLE IF NOT EXISTS agent_events (
    session_id VARCHAR NOT NULL REFERENCES sessions(id),
    session_seq INTEGER NOT NULL,
    run_id VARCHAR,
    event_type VARCHAR NOT NULL,
    durability VARCHAR NOT NULL,
    stream_id VARCHAR,
    stream_type VARCHAR,
    chunk_id INTEGER,
    is_complete INTEGER NOT NULL DEFAULT 0,
    parent_run_id VARCHAR,
    payload_json TEXT NOT NULL DEFAULT '{}',
    occurred_at VARCHAR NOT NULL,
    PRIMARY KEY(session_id, session_seq),
    UNIQUE(session_id, stream_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id, session_seq);

CREATE TABLE IF NOT EXISTS stream_snapshots (
    stream_id VARCHAR PRIMARY KEY,
    session_id VARCHAR NOT NULL REFERENCES sessions(id),
    run_id VARCHAR,
    stream_type VARCHAR NOT NULL DEFAULT 'answer',
    version INTEGER NOT NULL DEFAULT 0,
    last_chunk_id INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL DEFAULT '',
    content_length INTEGER NOT NULL DEFAULT 0,
    status VARCHAR NOT NULL DEFAULT 'streaming',
    updated_at VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    run_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    risk_level TEXT NOT NULL DEFAULT 'low',
    status TEXT NOT NULL DEFAULT 'pending',
    decision TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_session_status ON approvals(session_id, status);

CREATE TABLE IF NOT EXISTS tool_executions (
    tool_execution_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    run_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    retry_of_execution_id TEXT,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    arguments_fingerprint TEXT NOT NULL,
    redaction_policy_version TEXT NOT NULL DEFAULT 'tool-args-v1',
    fingerprint_key_version TEXT NOT NULL DEFAULT 'v1',
    side_effect_class TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_executions_tool_call ON tool_executions(tool_call_id, attempt);
CREATE INDEX IF NOT EXISTS idx_tool_executions_status ON tool_executions(status);
