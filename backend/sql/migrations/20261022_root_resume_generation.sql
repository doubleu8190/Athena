-- Root wait generations fence durable resume jobs across Plan rounds.
BEGIN;

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS root_wait_generation INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS root_wait_checkpoint_id VARCHAR,
    ADD COLUMN IF NOT EXISTS root_wait_armed_at VARCHAR;

COMMIT;
