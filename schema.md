
---

# `schema.sql`

```sql
-- DYC Communications Platform
-- Initial PostgreSQL schema v1

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =========================
-- ENUM TYPES
-- =========================

CREATE TYPE mailbox_provider AS ENUM (
  'gmail',
  'microsoft_365'
);

CREATE TYPE account_status AS ENUM (
  'active',
  'degraded',
  'reauth_required',
  'disabled'
);

CREATE TYPE sender_type AS ENUM (
  'human',
  'organization',
  'automated',
  'unknown'
);

CREATE TYPE primary_class AS ENUM (
  'human_direct',
  'health_family',
  'finance_money',
  'meetings_scheduling',
  'access_auth',
  'service_updates',
  'newsletters_news',
  'marketing_promotions',
  'notifications_system',
  'unknown_ambiguous'
);

CREATE TYPE priority_level AS ENUM (
  'critical',
  'important',
  'informational',
  'track_only',
  'needs_triage'
);

CREATE TYPE action_state AS ENUM (
  'respond',
  'review',
  'track',
  'archive',
  'delegate',
  'follow_up_later'
);

CREATE TYPE open_loop_state AS ENUM (
  'awaiting_user',
  'awaiting_other_party',
  'closed',
  'unknown'
);

CREATE TYPE action_status AS ENUM (
  'recommended',
  'approved',
  'rejected',
  'queued',
  'executed',
  'failed',
  'cancelled'
);

CREATE TYPE event_actor AS ENUM (
  'system',
  'user',
  'provider',
  'llm'
);

-- =========================
-- USERS
-- =========================

CREATE TABLE app_user (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  email TEXT NOT NULL UNIQUE,
  display_name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =========================
-- CONNECTED ACCOUNTS
-- =========================

CREATE TABLE connected_account (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,

  provider mailbox_provider NOT NULL,
  provider_account_id TEXT NOT NULL,
  email_address TEXT NOT NULL,
  display_name TEXT,

  status account_status NOT NULL DEFAULT 'active',

  -- Store encrypted reference or Key Vault pointer, not raw token when possible.
  token_secret_ref TEXT,
  scopes TEXT[] NOT NULL DEFAULT '{}',

  last_successful_sync_at TIMESTAMPTZ,
  last_sync_attempt_at TIMESTAMPTZ,
  sync_cursor TEXT,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE(provider, provider_account_id)
);

CREATE INDEX idx_connected_account_user ON connected_account(user_id);
CREATE INDEX idx_connected_account_status ON connected_account(status);

-- =========================
-- EMAIL THREADS
-- =========================

CREATE TABLE email_thread (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  account_id UUID NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,

  provider_thread_id TEXT NOT NULL,
  subject TEXT,
  normalized_subject TEXT,

  participants JSONB NOT NULL DEFAULT '[]',
  last_message_at TIMESTAMPTZ,
  message_count INTEGER NOT NULL DEFAULT 0,

  has_attachments BOOLEAN NOT NULL DEFAULT false,
  has_calendar_invite BOOLEAN NOT NULL DEFAULT false,

  last_ingested_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE(account_id, provider_thread_id)
);

CREATE INDEX idx_email_thread_account ON email_thread(account_id);
CREATE INDEX idx_email_thread_last_message ON email_thread(last_message_at DESC);
CREATE INDEX idx_email_thread_subject ON email_thread(normalized_subject);

-- =========================
-- EMAIL MESSAGES
-- =========================

CREATE TABLE email_message (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  thread_id UUID NOT NULL REFERENCES email_thread(id) ON DELETE CASCADE,
  account_id UUID NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,

  provider_message_id TEXT NOT NULL,
  provider_thread_id TEXT NOT NULL,

  from_address TEXT,
  from_name TEXT,
  to_addresses JSONB NOT NULL DEFAULT '[]',
  cc_addresses JSONB NOT NULL DEFAULT '[]',
  bcc_addresses JSONB NOT NULL DEFAULT '[]',

  subject TEXT,
  snippet TEXT,
  body_text TEXT,
  body_hash TEXT,

  received_at TIMESTAMPTZ,
  sent_at TIMESTAMPTZ,

  is_unread BOOLEAN NOT NULL DEFAULT false,
  is_from_me BOOLEAN NOT NULL DEFAULT false,
  has_attachments BOOLEAN NOT NULL DEFAULT false,
  has_calendar_invite BOOLEAN NOT NULL DEFAULT false,

  provider_labels JSONB NOT NULL DEFAULT '[]',
  provider_folders JSONB NOT NULL DEFAULT '[]',

  raw_metadata JSONB NOT NULL DEFAULT '{}',

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE(account_id, provider_message_id)
);

CREATE INDEX idx_email_message_thread ON email_message(thread_id);
CREATE INDEX idx_email_message_account_received ON email_message(account_id, received_at DESC);
CREATE INDEX idx_email_message_unread ON email_message(is_unread);
CREATE INDEX idx_email_message_from_address ON email_message(from_address);

-- =========================
-- ATTACHMENTS
-- =========================

CREATE TABLE email_attachment (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  message_id UUID NOT NULL REFERENCES email_message(id) ON DELETE CASCADE,

  provider_attachment_id TEXT,
  filename TEXT,
  mime_type TEXT,
  size_bytes BIGINT,

  storage_ref TEXT,
  content_hash TEXT,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_attachment_message ON email_attachment(message_id);

-- =========================
-- CLASSIFICATION RESULTS
-- =========================

CREATE TABLE thread_classification (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  thread_id UUID NOT NULL REFERENCES email_thread(id) ON DELETE CASCADE,

  primary_class primary_class NOT NULL,
  priority priority_level NOT NULL,
  action action_state NOT NULL,
  confidence NUMERIC(4,3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),

  sender_type sender_type NOT NULL DEFAULT 'unknown',
  time_sensitivity TEXT NOT NULL DEFAULT 'none_detected',

  has_explicit_ask BOOLEAN NOT NULL DEFAULT false,
  has_deadline BOOLEAN NOT NULL DEFAULT false,
  deadline_text TEXT,

  domain_signals TEXT[] NOT NULL DEFAULT '{}',
  reasoning_summary TEXT,

  parking_destination TEXT,
  needs_digest BOOLEAN NOT NULL DEFAULT false,
  needs_user_alert BOOLEAN NOT NULL DEFAULT false,

  open_loop_state open_loop_state NOT NULL DEFAULT 'unknown',

  classifier_version TEXT NOT NULL,
  rules_version TEXT,
  llm_model TEXT,

  is_current BOOLEAN NOT NULL DEFAULT true,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_classification_thread ON thread_classification(thread_id);
CREATE INDEX idx_classification_current ON thread_classification(thread_id, is_current);
CREATE INDEX idx_classification_priority ON thread_classification(priority);
CREATE INDEX idx_classification_primary_class ON thread_classification(primary_class);
CREATE INDEX idx_classification_open_loop ON thread_classification(open_loop_state);

-- Ensure only one current classification per thread.
CREATE UNIQUE INDEX uq_current_classification
ON thread_classification(thread_id)
WHERE is_current = true;

-- =========================
-- RECOMMENDED / APPROVED ACTIONS
-- =========================

CREATE TABLE mailbox_action (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

  thread_id UUID REFERENCES email_thread(id) ON DELETE CASCADE,
  message_id UUID REFERENCES email_message(id) ON DELETE CASCADE,
  account_id UUID NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,

  action_type TEXT NOT NULL,
  action_payload JSONB NOT NULL DEFAULT '{}',

  status action_status NOT NULL DEFAULT 'recommended',

  recommended_by TEXT NOT NULL DEFAULT 'system',
  approved_by UUID REFERENCES app_user(id),
  approved_at TIMESTAMPTZ,

  executed_at TIMESTAMPTZ,
  failure_reason TEXT,

  idempotency_key TEXT NOT NULL UNIQUE,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CHECK (thread_id IS NOT NULL OR message_id IS NOT NULL)
);

CREATE INDEX idx_mailbox_action_status ON mailbox_action(status);
CREATE INDEX idx_mailbox_action_account ON mailbox_action(account_id);
CREATE INDEX idx_mailbox_action_thread ON mailbox_action(thread_id);

-- =========================
-- DRAFT REPLIES
-- =========================

CREATE TABLE draft_reply (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  thread_id UUID NOT NULL REFERENCES email_thread(id) ON DELETE CASCADE,
  account_id UUID NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,

  draft_subject TEXT,
  draft_body TEXT NOT NULL,

  tone TEXT,
  generated_by_model TEXT,
  generation_prompt_version TEXT,

  provider_draft_id TEXT,
  is_pushed_to_provider BOOLEAN NOT NULL DEFAULT false,

  created_by UUID REFERENCES app_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_draft_reply_thread ON draft_reply(thread_id);

-- =========================
-- DIGESTS
-- =========================

CREATE TABLE digest (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,

  digest_date DATE NOT NULL,
  digest_type TEXT NOT NULL,

  title TEXT,
  summary TEXT,
  payload JSONB NOT NULL DEFAULT '{}',

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE(user_id, digest_date, digest_type)
);

CREATE INDEX idx_digest_user_date ON digest(user_id, digest_date DESC);

-- =========================
-- AUDIT EVENTS
-- =========================

CREATE TABLE audit_event (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

  user_id UUID REFERENCES app_user(id) ON DELETE SET NULL,
  account_id UUID REFERENCES connected_account(id) ON DELETE SET NULL,
  thread_id UUID REFERENCES email_thread(id) ON DELETE SET NULL,
  message_id UUID REFERENCES email_message(id) ON DELETE SET NULL,
  mailbox_action_id UUID REFERENCES mailbox_action(id) ON DELETE SET NULL,

  actor event_actor NOT NULL,
  event_type TEXT NOT NULL,

  before_state JSONB,
  after_state JSONB,
  metadata JSONB NOT NULL DEFAULT '{}',

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_event_user_time ON audit_event(user_id, created_at DESC);
CREATE INDEX idx_audit_event_thread_time ON audit_event(thread_id, created_at DESC);
CREATE INDEX idx_audit_event_type ON audit_event(event_type);

-- =========================
-- USER CORRECTIONS / FEEDBACK
-- =========================

CREATE TABLE classification_feedback (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

  user_id UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  thread_id UUID NOT NULL REFERENCES email_thread(id) ON DELETE CASCADE,
  classification_id UUID REFERENCES thread_classification(id) ON DELETE SET NULL,

  original_primary_class primary_class,
  corrected_primary_class primary_class,

  original_priority priority_level,
  corrected_priority priority_level,

  original_action action_state,
  corrected_action action_state,

  feedback_note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_feedback_thread ON classification_feedback(thread_id);
CREATE INDEX idx_feedback_user ON classification_feedback(user_id);

-- =========================
-- SYNC JOBS
-- =========================

CREATE TABLE sync_job (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

  account_id UUID NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,
  job_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',

  cursor_before TEXT,
  cursor_after TEXT,

  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,

  attempt_count INTEGER NOT NULL DEFAULT 0,
  failure_reason TEXT,

  idempotency_key TEXT NOT NULL UNIQUE,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sync_job_account_status ON sync_job(account_id, status);

-- =========================
-- UPDATED_AT TRIGGER
-- =========================

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_app_user_updated
BEFORE UPDATE ON app_user
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_connected_account_updated
BEFORE UPDATE ON connected_account
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_email_thread_updated
BEFORE UPDATE ON email_thread
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_email_message_updated
BEFORE UPDATE ON email_message
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_mailbox_action_updated
BEFORE UPDATE ON mailbox_action
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_draft_reply_updated
BEFORE UPDATE ON draft_reply
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_sync_job_updated
BEFORE UPDATE ON sync_job
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
