CREATE INDEX IF NOT EXISTS idx_agent_events_session_run_seq
    ON agent_events (session_id, run_id, session_seq);
