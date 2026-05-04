CREATE TABLE IF NOT EXISTS motion_task_sync (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,
    account_email TEXT NOT NULL,
    provider_message_id TEXT NOT NULL,
    motion_task_id TEXT,
    motion_task_name TEXT,
    motion_priority TEXT,
    status TEXT NOT NULL,
    error TEXT,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(account_id, provider_message_id)
);

CREATE INDEX IF NOT EXISTS idx_motion_task_sync_account_status
    ON motion_task_sync(account_id, status);
