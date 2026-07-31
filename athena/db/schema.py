"""数据库初始化 — 建表 DDL."""

from __future__ import annotations

# 注意：当前开发阶段暂不实现数据库迁移机制，启动时直接建表。
# sessions 表包含 7.3.4 扩展的状态字段（status / run_id）以支持中断恢复。
_SCHEMA = """
-- 会话表（含中断恢复状态）
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New Session',
    status TEXT NOT NULL DEFAULT 'idle',
    run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

-- 消息表
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT DEFAULT '[]',
    tool_call_id TEXT,
    metadata TEXT DEFAULT '{}',
    timestamp TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);

-- 执行步骤表
-- step_number 在同一 run_id 范围内全局递增
-- 每个 LLM 调用和每个工具执行都是独立 step
CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    parent_step_id TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms REAL DEFAULT 0,
    llm_input_tokens INTEGER DEFAULT 0,
    llm_output_tokens INTEGER DEFAULT 0,
    error_message TEXT,
    metadata TEXT DEFAULT '{}',
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_steps_session ON steps(session_id);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id);
CREATE INDEX IF NOT EXISTS idx_steps_number ON steps(step_number);
CREATE INDEX IF NOT EXISTS idx_steps_parent ON steps(parent_step_id);

-- 工具调用记录表
CREATE TABLE IF NOT EXISTS tool_call (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT DEFAULT '{}',
    raw_output TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms REAL DEFAULT 0,
    error_message TEXT,
    error_stack TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (step_id) REFERENCES steps(id)
);
CREATE INDEX IF NOT EXISTS idx_tool_call_session ON tool_call(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_call_step ON tool_call(step_id);
CREATE INDEX IF NOT EXISTS idx_tool_call_status ON tool_call(status);

-- 审批日志表
CREATE TABLE IF NOT EXISTS approval_logs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT DEFAULT '{}',
    risk_level TEXT NOT NULL,
    decision TEXT NOT NULL,
    decision_time_ms REAL,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_approval_logs_session ON approval_logs(session_id);
"""


def get_schema() -> str:
    """返回建表 SQL."""
    return _SCHEMA
