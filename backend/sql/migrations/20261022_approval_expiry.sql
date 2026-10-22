CREATE INDEX IF NOT EXISTS idx_approvals_pending_expiry
    ON approvals(expires_at, created_at)
    WHERE status = 'pending' AND expires_at IS NOT NULL;
