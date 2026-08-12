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
	step_id VARCHAR, 
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

CREATE TABLE tool_call (
	id VARCHAR NOT NULL, 
	session_id VARCHAR NOT NULL, 
	step_id VARCHAR NOT NULL, 
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
CREATE INDEX idx_tool_call_status ON tool_call (status);
CREATE INDEX idx_tool_call_session ON tool_call (session_id);
CREATE INDEX idx_tool_call_step ON tool_call (step_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    memory_id UNINDEXED,
    tokenize='unicode61'
);

PRAGMA user_version = 1;
