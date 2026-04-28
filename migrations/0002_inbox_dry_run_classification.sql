-- DYC Communications Platform
-- Migration 0002: dry-run inbox classification log
--
-- Stores one row per message considered by the dry-run classifier so an
-- operator can review the recommendations before any mailbox-mutating
-- behavior is built. Strictly non-destructive: nothing in this table
-- represents an action that was actually taken.
--
-- Note on schema parity: the runtime bootstrap in apps/api/app/main.py
-- creates this table with TEXT id columns (matching the runtime variant
-- of connected_account). This file mirrors that runtime shape so a fresh
-- DB built from migrations behaves identically to one bootstrapped by
-- the API.

CREATE TABLE IF NOT EXISTS inbox_dry_run_classification (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,
    account_email TEXT NOT NULL,

    provider_message_id TEXT NOT NULL,
    received_at TIMESTAMPTZ,
    sender TEXT,
    subject TEXT,
    current_folder TEXT,

    recommended_folder TEXT NOT NULL,
    category TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    confidence_band TEXT NOT NULL,
    forced_review BOOLEAN NOT NULL DEFAULT false,
    reasons TEXT[] NOT NULL DEFAULT '{}',
    safety_flags TEXT[] NOT NULL DEFAULT '{}',

    provider_consulted BOOLEAN NOT NULL DEFAULT false,
    provider_name TEXT,

    status TEXT NOT NULL DEFAULT 'classified',
    error TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(account_id, provider_message_id)
);

CREATE INDEX IF NOT EXISTS idx_inbox_dry_run_account_received
    ON inbox_dry_run_classification(account_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_inbox_dry_run_account_email_created
    ON inbox_dry_run_classification(account_email, created_at DESC);
