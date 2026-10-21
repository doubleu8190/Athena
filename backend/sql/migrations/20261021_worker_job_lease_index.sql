-- Index expired durable Worker leases for bounded recovery scans.
BEGIN;

CREATE INDEX IF NOT EXISTS idx_worker_jobs_lease
    ON worker_jobs(status, lease_until);

COMMIT;
