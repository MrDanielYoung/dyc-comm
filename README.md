# DYC Communications Platform (DYC_Comm)

## Product + Architecture Specification (v1)

Related planning docs:

* [docs/local-setup.md](docs/local-setup.md) - run the API locally, required env vars, secret handling rules
* [docs/mvp-account-strategy.md](docs/mvp-account-strategy.md) - Microsoft-first MVP scope, managed accounts, folder policy, and backlog
* [docs/working-sop.md](docs/working-sop.md) - verification-first workflow to reduce avoidable errors and unverified assumptions
* [docs/github-cicd.md](docs/github-cicd.md) - GitHub Actions, Azure OIDC, and deployment workflow setup
* [docs/ai-classifier-policy.md](docs/ai-classifier-policy.md) - dry-run AI classifier contract, Azure OpenAI/Azure AI provider choice, data minimization, BIOTRONIK posture, human-in-the-loop controls
* [docs/inbox-dryrun.md](docs/inbox-dryrun.md) - operator-triggered, non-destructive inbox dry-run classification (endpoints, CLI, manual validation steps)

---

## 1. Overview

DYC_Comm is a **mission-critical communications control plane** designed to act as the central nexus for all business communications.

The platform ingests, classifies, prioritizes, and routes communications across multiple accounts (Gmail, Microsoft 365 initially), while preserving those providers as systems of record.

DYC_Comm is **not an email client in v1**. It is a **triage, orchestration, and decision-support system** that:

* surfaces what matters
* suppresses noise
* tracks obligations
* assists with responses
* maintains auditability

---

## 2. Core Objectives

### Primary Objective

Create a unified system that ensures:

> No critical communication is missed, delayed, or lost in noise.

### Secondary Objectives

* Reduce inbox overload across multiple accounts
* Provide actionable prioritization (not just sorting)
* Enable controlled automation (human-in-the-loop)
* Maintain full audit trail of decisions and actions
* Serve as a foundation for future communications channels (calendar, SMS, Teams)

---

## 3. System Boundaries

### In Scope (v1)

* Email ingestion (Gmail + Microsoft 365)
* Thread-level classification
* Priority scoring and routing
* Dashboard (Immediate, Today, Digest, Triage)
* Draft response generation
* Labeling / moving (human-approved only)
* Audit logging

### Out of Scope (v1)

* Full email client replacement
* Automatic email sending
* Permanent deletion automation
* Real-time collaboration features
* Non-email channels (future phase)

---

## 4. Reliability and SLA Targets

### SLA Target (v1)

* **99.0% uptime (monthly)**

### SLO Targets

* API availability: ≥ 99.0%
* Message ingestion latency: < 60 seconds (p95)
* Classification latency: < 5 seconds per thread (p95)
* Dashboard load time: < 2 seconds (p95)

### Error Budget

* ~7.2 hours downtime per month acceptable (hard ceiling)

---

## 5. Reliability Requirements

### 5.1 No Single Point of Failure

* All services must be horizontally scalable
* Stateless services where possible
* Managed database with high availability enabled

### 5.2 Graceful Degradation

System must remain usable if:

* LLM provider is unavailable → fallback to rule-based triage
* Mail APIs are degraded → show last known state + retry queue
* Classification fails → route to “Needs Triage”

### 5.3 Provider Independence

* Gmail and Microsoft 365 remain systems of record
* DYC_Comm stores metadata, not authoritative mail state

### 5.4 Idempotent Processing

* All ingestion and classification must be replayable
* No duplicate actions on retry

### 5.5 Observability

* Centralized logging
* Metrics for ingestion, classification, errors
* Alerting on:

  * ingestion failure
  * queue backlog growth
  * API downtime

---

## 6. High-Level Architecture

### Core Components

#### 6.1 Control Plane (`DYC_Comm_ControlPlane`)

* Account linking (OAuth)
* User settings
* Classification policy
* Permissions and roles

#### 6.2 Email Core (`DYC_Comm_EmailCore`)

* Email ingestion
* Thread normalization
* Classification engine
* Priority scoring
* Action suggestion

#### 6.3 Connectors (`DYC_Comm_Connectors`)

* Gmail API integration
* Microsoft Graph integration
* Future: Phone.com, others

#### 6.4 Digest Service (`DYC_Comm_Digest`)

* Daily summaries
* Newsletter aggregation
* Informational rollups

#### 6.5 Scheduling Core (`DYC_Comm_ScheduleCore`)

* Meeting extraction
* Calendar correlation
* Schedule awareness

#### 6.6 Auth Relay (`DYC_Comm_AuthRelay`)

* OTP detection
* Magic link prioritization
* SMS relay ingestion

#### 6.7 Audit Service (`DYC_Comm_Audit`)

* Immutable event log
* Classification history
* Action tracking
* User overrides

---

## 7. Infrastructure (Azure)

### Compute

* Azure Container Apps (API + workers)
* Container Apps Jobs (backlog processing)

### Data

* Azure Database for PostgreSQL (HA enabled)

### Messaging

* Azure Service Bus (queues + topics)

### Secrets

* Azure Key Vault

### Edge / Routing

* Azure Front Door

### Monitoring

* Azure Monitor + Application Insights

---

## 8. Data Model (Conceptual)

### Thread Entity

* thread_id
* account_id
* subject
* participants
* last_message_timestamp
* normalized_content

### Classification

* primary_class
* priority
* action
* confidence
* reasoning_summary

### Event Log

* event_id
* timestamp
* event_type
* actor (system/user)
* before_state
* after_state

---

## 9. Classification System

### Primary Classes

* human_direct
* health_family
* finance_money
* meetings_scheduling
* access_auth
* service_updates
* newsletters_news
* marketing_promotions
* notifications_system
* unknown_ambiguous

### Priority Levels

* critical
* important
* informational
* track_only
* needs_triage

### Actions

* respond
* review
* track
* archive
* delegate
* follow_up_later

---

## 10. Core User Experience

### 10.1 Immediate Queue

* Critical messages only
* Must be reviewed same day

### 10.2 Today Queue

* Important messages
* Non-interruptive but required

### 10.3 Digest View

* Newsletters
* Notifications
* Informational content

### 10.4 Needs Triage

* Low-confidence classifications
* Unknown senders

### 10.5 Open Loops

* Awaiting response from user
* Awaiting response from others

---

## 11. Safety and Control Model

### v1 Constraints

* No automatic sending
* No permanent deletion
* No autonomous actions on ambiguous emails

### Human-in-the-Loop

* All mailbox actions require approval initially
* Confidence thresholds gate automation

---

## 12. Development Phases

### Phase 0 – Foundation

* Repo setup
* Basic architecture
* Local dev environment

### Phase 1 – Read-Only Ingestion

* Gmail + Microsoft integration
* Thread ingestion
* Database persistence

### Phase 2 – Classification + Dashboard

* Classification engine
* UI queues
* Basic triage

### Phase 3 – Suggested Actions

* Draft replies
* Recommended moves/labels
* Manual execution

### Phase 4 – Controlled Automation

* Safe auto-labeling
* Newsletter suppression
* Digest generation

---

## 13. Non-Functional Requirements

* Secure OAuth handling
* Encrypted storage for tokens
* Full auditability
* Deterministic fallback behavior
* Scalable to multiple accounts and high email volume

---

## 14. Guiding Principle

> DYC_Comm is not an inbox.
> It is a decision engine for communications.

The system must prioritize **clarity, control, and reliability over automation complexity**.

---

## 15. Deliverables for v1

* architecture.md
* schema.sql
* API specification
* Backend scaffold (Python)
* Frontend scaffold (React/Next.js)
* Classification engine (LLM + rules)
* Dashboard UI
* Deployment configuration (Azure)

---
