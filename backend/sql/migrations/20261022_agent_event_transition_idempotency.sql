CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_events_session_transition_id
    ON agent_events (session_id, transition_id)
    WHERE transition_id IS NOT NULL;
