-- DYC Communications Platform
-- Migration: 0002 — message-sync instrumentation
--
-- Adds the three tables that the API's lazy bootstrap path
-- (apps/api/app/main.py::_ensure_account_tables) creates so that the
-- operations dashboard can compute real "messages seen", "processed", and
-- "errors" counters without falling back to placeholder/empty metrics.
--
-- The lazy bootstrap uses TEXT primary keys (uuid_generate_v4 from psycopg)
-- and TEXT-typed account_id columns to align with the existing
-- connected_account / mailbox_folder rows the runtime already creates that
-- way. This file mirrors that exactly so apply order does not matter.
--
-- Idempotent: every CREATE uses IF NOT EXISTS so re-running is safe.

-- =========================
-- sync_event
-- =========================

CREATE TABLE IF NOT EXISTS sync_event (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    folders_seen INTEGER,
    messages_seen INTEGER,
    messages_persisted INTEGER,
    messages_moved INTEGER,
    errors INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sync_event_account_time
ON sync_event(account_id, completed_at DESC);

CREATE INDEX IF NOT EXISTS idx_sync_event_operation_time
ON sync_event(operation, completed_at DESC);

-- =========================
-- message_sighting
-- =========================
--
-- Per-message observation written by POST /mail/messages/sync. Subjects are
-- truncated by the runtime to SUBJECT_PREVIEW_MAX_CHARS (120 today); body
-- text is never fetched or stored.

CREATE TABLE IF NOT EXISTS message_sighting (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,
    provider_message_id TEXT NOT NULL,
    folder_provider_id TEXT,
    folder_display_name TEXT,
    subject_preview TEXT,
    received_at TIMESTAMPTZ,
    is_unread BOOLEAN,
    has_attachments BOOLEAN,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(account_id, provider_message_id)
);

CREATE INDEX IF NOT EXISTS idx_message_sighting_account_received
ON message_sighting(account_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_message_sighting_account_seen
ON message_sighting(account_id, first_seen_at DESC);

-- =========================
-- mailbox_action_event
-- =========================
--
-- Instrumentation seam for the not-yet-built move/classification worker.
-- Today the runtime creates the table but no code path writes to it; the
-- dashboard reads zero counts honestly until the worker lands.

CREATE TABLE IF NOT EXISTS mailbox_action_event (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,
    provider_message_id TEXT,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    source_folder TEXT,
    target_folder TEXT,
    error_message TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mailbox_action_event_account_time
ON mailbox_action_event(account_id, occurred_at DESC);
