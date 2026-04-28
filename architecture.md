# DYC Communications Platform — Architecture

## 1. Purpose

DYC_Comm is a mission-critical communications control plane for business email and related communications workflows.

The platform ingests messages from multiple mailbox providers, classifies and prioritizes threads, surfaces action queues, assists with drafting, and records all user/system actions in an auditable event log.

DYC_Comm is not a full replacement email client in v1. Gmail and Microsoft 365 remain the systems of record.

## 2. Reliability Target

Target uptime: 99.0% monthly.

The system must continue to function in degraded mode if:
- LLM provider is unavailable
- Gmail or Microsoft Graph APIs are temporarily unavailable
- classification fails
- background queues are delayed

## 3. Core Principles

1. Do not miss critical communications.
2. Bias toward over-escalation rather than false negatives.
3. Keep mailbox providers as systems of record.
4. Use AI for prioritization, summarization, and drafting, not uncontrolled execution.
5. Every classification and mailbox action must be logged.
6. v1 must be human-in-the-loop for all mailbox-changing actions.

## 4. Service Boundaries

### DYC_Comm_ControlPlane
Responsibilities:
- user/account settings
- OAuth account linking
- mailbox provider registration
- system configuration
- feature flags
- confidence thresholds

### DYC_Comm_EmailCore
Responsibilities:
- message ingestion
- thread normalization
- classification
- priority scoring
- recommended actions
- open-loop detection

### DYC_Comm_Connectors
Responsibilities:
- Gmail API integration
- Microsoft Graph integration
- provider abstraction layer
- sync cursors
- retry handling
- provider-specific message actions

### DYC_Comm_Digest
Responsibilities:
- daily summaries
- newsletter rollups
- noise reporting
- service update digests

### DYC_Comm_ScheduleCore
Responsibilities:
- meeting detection
- invite parsing
- calendar correlation
- same-day schedule risk detection

### DYC_Comm_AuthRelay
Responsibilities:
- OTP/magic link detection
- sign-in alert classification
- stale-code demotion
- phone.com email-to-SMS relay support later

### DYC_Comm_Audit
Responsibilities:
- immutable event log
- classification history
- user override tracking
- mailbox action logs

## 5. Azure Deployment

Recommended v1 Azure services:

- Azure Container Apps
  - API service
  - worker service
  - scheduler/backlog jobs

- Azure Database for PostgreSQL Flexible Server
  - high availability enabled for production

- Azure Service Bus
  - ingestion queue
  - classification queue
  - action queue
  - digest queue

- Azure Key Vault
  - OAuth secrets
  - API keys
  - encryption secrets

- Azure Front Door
  - public entry point
  - TLS termination
  - basic global routing

- Azure Monitor / Application Insights
  - logs
  - metrics
  - traces
  - alerts

## 6. Logical Architecture

User Browser
→ Front Door
→ Web/API Service
→ PostgreSQL
→ Service Bus
→ Worker Services
→ Gmail / Microsoft Graph APIs
→ LLM Provider

## 7. Core Data Flow

### 7.1 Account Linking
1. User links Gmail or Microsoft 365 account.
2. OAuth tokens are exchanged.
3. Refresh tokens are stored securely via encrypted storage and/or Key Vault-backed secrets.
4. Account metadata is stored in PostgreSQL.
5. Initial sync job is queued.

### 7.2 Message Ingestion
1. Connector fetches messages and threads.
2. Raw provider metadata is normalized.
3. Message/thread records are upserted.
4. Thread classification job is queued.
5. Sync cursor is persisted.

### 7.3 Classification
1. Worker retrieves thread.
2. Deterministic rules run first.
3. LLM classifier runs if required.
4. Result is persisted.
5. Recommended route/action is generated.
6. Audit event is recorded.

### 7.4 User Review
1. User opens dashboard.
2. API returns queues:
   - Immediate
   - Today
   - Needs Triage
   - Digest
   - Open Loops
3. User approves or rejects recommended actions.
4. Approved mailbox actions are queued.
5. Connector executes provider-specific action.
6. Audit event is recorded.

## 8. Classification Taxonomy

### Primary Classes
- human_direct
- health_family
- finance_money
- meetings_scheduling
- access_auth
- service_updates
- newsletters_news
- marketing_promotions
- notifications_system
- unknown_ambiguous

### Priority Levels
- critical
- important
- informational
- track_only
- needs_triage

### Action States
- respond
- review
- track
- archive
- delegate
- follow_up_later

## 9. Safety Rules

### v1 Allowed
- read email metadata and content
- classify threads
- summarize threads
- propose actions
- generate draft replies
- manually approved label/move/archive actions

### v1 Not Allowed
- automatic sending
- permanent deletion
- auto-archiving low-confidence human mail
- suppressing health, finance, meeting, or access mail without review

### Conservative / Fallback Folder

`10 - Review` is the canonical fallback folder for AI routing. Whenever the
classifier is uncertain, or the message falls into a category that requires
human judgment, the message is routed to `10 - Review` (or left in `Inbox`)
rather than guessed into a more specific folder. Categories that always
route to `10 - Review` regardless of confidence:

- ambiguous business emails
- legal or contractual emails not unambiguously matched to `70 - Contracts`
- sensitive customer-adjacent or patient-adjacent content
- short emails without enough context to classify confidently
- threads where the newest message changes the meaning of earlier messages
- messages requiring judgment about tone, politics, or obligations

See docs/mvp-account-strategy.md → "AI Routing And Safety Policy" for the
full rule set and rationale.

## 10. Confidence Thresholds

- >= 0.90: safe for non-destructive auto-labeling later
- 0.70–0.89: route to queue and include in digest
- 0.50–0.69: route to `10 - Review` or Needs Triage; never guess a specific numbered folder
- < 0.50: route to `10 - Review` (or Needs Triage)

In v1, all mailbox-changing actions require approval regardless of confidence. Low-confidence classifications never auto-route to a specific numbered folder — they go to `10 - Review`, and the AI never auto-deletes or auto-sends.

## 11. Provider Abstraction

Use a common internal interface:

- list_accounts()
- sync_account(account_id)
- get_thread(account_id, provider_thread_id)
- apply_label(account_id, message_or_thread_id, label)
- move_message(account_id, message_id, destination)
- archive_message(account_id, message_id)
- create_draft_reply(account_id, thread_id, body)

Provider-specific behavior:
- Gmail: labels are primary; archiving removes INBOX label.
- Microsoft 365: folders/categories are primary; moving uses Microsoft Graph.

## 12. Background Queues

### ingestion.queue
For fetching new and changed messages.

### classification.queue
For classifying or reclassifying threads.

### action.queue
For executing approved mailbox actions.

### digest.queue
For generating daily summaries.

### audit.queue
Optional; audit writes can also be synchronous for critical events.

## 13. Idempotency

All jobs must include:
- job_id
- account_id
- provider
- provider_message_id or provider_thread_id
- idempotency_key
- attempt_count

Mailbox actions must be idempotent where possible.

Do not execute duplicate mailbox actions if the same approved action already succeeded.

## 14. Fallback Behavior

### LLM unavailable
- use deterministic rules only
- route uncertain threads to Needs Triage

### Provider unavailable
- show last known state
- mark account sync as degraded
- retry with exponential backoff

### Database unavailable
- API should fail closed
- no mailbox actions should execute without audit persistence

### Queue backlog
- dashboard should surface delayed-processing warning

## 15. Observability

Track:
- account sync success/failure
- ingestion latency
- classification latency
- classification failure rate
- action execution success/failure
- queue depth
- LLM error rate
- API uptime
- user override rate

Alerts:
- provider sync failure > 15 minutes
- classification failure rate > 5%
- action failure rate > 2%
- queue backlog above threshold
- API unavailable
- database unavailable

## 16. MVP Milestones

### Milestone 0 — Repository + Local Dev
- monorepo scaffold
- backend service
- frontend app
- local PostgreSQL
- local queue emulator or simple worker loop
- environment config

### Milestone 1 — Read-Only Ingestion
- Gmail OAuth
- Microsoft OAuth
- normalized account/message/thread tables
- manual sync endpoint
- basic inbox/thread viewer

### Milestone 2 — Classification + Dashboard
- deterministic rules
- LLM classifier contract
- Immediate queue
- Today queue
- Needs Triage queue
- Digest buckets

### Milestone 3 — Human-Approved Actions
- recommended actions
- approve/reject action UI
- Gmail label/archive
- Microsoft move/archive
- audit trail

### Milestone 4 — Digest + Open Loops
- daily digest
- open-loop detection
- waiting-on-me / waiting-on-others view

## 17. Recommended Repo Structure

```text
dyc-comm-platform/
  apps/
    web/
      package.json
      src/
    api/
      pyproject.toml
      src/
        main.py
        routes/
        services/
        models/
        db/
        workers/
        connectors/
        classifiers/
        audit/
  infra/
    azure/
    docker/
  docs/
    architecture.md
    api.md
    classifier-policy.md
  sql/
    schema.sql
  tests/
    fixtures/
    integration/
