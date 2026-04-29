-- DYC Communications Platform
-- Migration 0003: persist dry-run move execution metadata
--
-- Extends inbox_dry_run_classification with the columns needed to record
-- a human-approved move of a single message into the recommended folder.
-- We deliberately keep the audit-like fields on the same row instead of
-- introducing a new table: each dry-run row already represents a single
-- (account_id, provider_message_id) pair and the move targets exactly
-- that pair, so colocating execution status keeps the read path simple
-- and matches what the runtime bootstrap in apps/api/app/main.py creates.
--
-- Status values used by the API:
--   - 'classified'   : initial dry-run row, no move attempted
--   - 'moving'       : reserved for future async workers (not used yet)
--   - 'moved'        : Graph move succeeded, fields below are populated
--   - 'move_failed'  : Graph move was attempted and failed; action_error
--                      carries the human-readable reason
--
-- The new columns are nullable so existing dry-run rows from migration
-- 0002 continue to read back unchanged.

ALTER TABLE inbox_dry_run_classification
    ADD COLUMN IF NOT EXISTS executed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS executed_to_folder TEXT,
    ADD COLUMN IF NOT EXISTS executed_provider_folder_id TEXT,
    ADD COLUMN IF NOT EXISTS executed_provider_message_id TEXT,
    ADD COLUMN IF NOT EXISTS action_error TEXT;
