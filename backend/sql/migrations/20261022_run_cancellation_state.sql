-- Keep uncertain external side effects visible after a Run is stopped.
BEGIN;

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS cancellation_external_effect_status VARCHAR NOT NULL DEFAULT 'none';

COMMIT;
