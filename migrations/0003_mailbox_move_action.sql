-- DYC Communications Platform
-- Migration 0003: approved mailbox move actions (audit + idempotency log)
--
-- One row per explicit human-approved move from the dry-run UI/CLI/API.
-- Logs the source dry-run row (when present), the resolved target folder,
-- the Graph call outcome, and the destination folder id we asked Graph to
-- move to. Combined with the (account_id, provider_message_id, status)
-- check this gives:
--
-- * idempotency — re-issuing a move for an already-moved message returns
--   the existing 'succeeded' row instead of double-moving.
-- * audit — every move attempt persists with status, timestamp, and any
--   error so the activity log can render an honest history.
--
-- This table holds intent and outcome only. The mailbox itself is the
-- system of record — DYC_Comm never claims to own where a message lives.
--
-- Schema parity: the runtime bootstrap in apps/api/app/main.py creates
-- this table with TEXT id columns (matching the rest of the runtime
-- schema). This file mirrors that runtime shape.

CREATE TABLE IF NOT EXISTS mailbox_move_action (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,
    account_email TEXT NOT NULL,

    provider_message_id TEXT NOT NULL,
    source_folder_id TEXT,
    destination_folder_id TEXT,
    destination_folder_name TEXT NOT NULL,

    dry_run_classification_id TEXT REFERENCES inbox_dry_run_classification(id)
        ON DELETE SET NULL,
    forced_review BOOLEAN NOT NULL DEFAULT false,

    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,

    requested_by_email TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mailbox_move_action_account_requested
    ON mailbox_move_action(account_id, requested_at DESC);

CREATE INDEX IF NOT EXISTS idx_mailbox_move_action_account_message_status
    ON mailbox_move_action(account_id, provider_message_id, status);
