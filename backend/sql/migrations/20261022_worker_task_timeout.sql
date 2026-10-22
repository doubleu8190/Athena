ALTER TABLE agent_tasks
    ADD COLUMN IF NOT EXISTS timeout_seconds DOUBLE PRECISION NOT NULL DEFAULT 3600;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_agent_tasks_timeout_positive'
          AND conrelid = 'agent_tasks'::regclass
    ) THEN
        ALTER TABLE agent_tasks
            ADD CONSTRAINT ck_agent_tasks_timeout_positive
            CHECK (timeout_seconds > 0);
    END IF;
END $$;
