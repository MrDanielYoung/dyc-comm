# MVP Account Strategy

## Purpose

This document captures the initial operating plan for the first live version of DYC Comm.

The goal of the MVP is not to fully automate mailbox management across all accounts. The goal is to prove that DYC Comm can safely reduce inbox noise for a single Microsoft 365 business account by moving low-risk emails into a very small set of destination folders.

## MVP Recommendation

### Provider

Start with Microsoft 365, not Gmail.

Reasons:

* the most valuable mailboxes for this project are Microsoft-based
* the target business workflows live in Microsoft accounts
* it is better to solve the real business case first instead of tuning against low-value personal Gmail traffic

### First Live Mailbox

Start with:

* `daniel@danielyoung.io`

Use this mailbox as the proving ground for live move-actions.

Rationale:

* it is business-relevant
* it is lower-risk than the main DHW mailbox
* it allows policy tuning before rollout into the highest-value inbox

### Second Rollout Mailbox

After the rules are stable, add:

* `daniel.young@digitalhealthworks.com`

This should be treated as the production-grade rollout, not the experimental one.

## Managed Accounts

Current account set:

* `DHW`: `daniel.young@digitalhealthworks.com`
* `Personal business`: `daniel@danielyoung.io`
* `Boldworks DE`: `danie.young@boldworks.de`
* `Boldworks LLC`: `daniel.young@boldworks.llc`
* `Gmail`: `daniel@danielyoung.de`

Notes:

* `Boldworks DE` is expected to be deprecated as the business transitions to DHW
* `Boldworks LLC` should ultimately be merged into `danielyoung.io`
* Gmail is not part of the core business-mail MVP
* confirm whether `danie.young@boldworks.de` is intentionally spelled without the second `l`

## MVP Folder Model

Keep the folder structure intentionally lean.

Folders:

* `Inbox`
* `Review`
* `News`
* `Notifications`

Alternative Folder Naming, using numeber scheme to sort order:

* `Inbox` (Part of the standard inbox)
* `20 - Review`
* `30 - News`
* `40 - Notifications`

* Gaps in the numbering are for future use cases and additional boxes after MVP.

Interpretation:

* `Inbox` means the message may require attention, reply, decision, or manual review soon
* `Review` means worth reading later, but not urgent and not part of the designated news stream
* `News` means approved publication/news sources only
* `Notifications` means informational mail that should not consume inbox space

## MVP Move Policy

### Keep In Inbox

Messages should remain in `Inbox` if they are:

* direct human email
* messages with an explicit ask
* messages requiring a decision or reply
* meetings, invites, or scheduling threads
* finance, legal, health, access, or security-related mail
* VIP sender mail
* ambiguous or low-confidence classifications

Default bias:

* when uncertain, keep the message in `Inbox`

### Move To News

Move a message to `News` only when it clearly comes from an approved publication or designated news source.

Examples mentioned so far:

* Bloomberg
* New York Times
* Financial Times
* Wall Street Journal
* STAT+
* Health Affairs
* Wired

Rules:

* only move to `News` from an allowlist of approved senders/domains
* if a source is not on the allowlist, do not place it in `News`

Future feature:

* prepare a daily summary from the `News` folder with links back to source articles

### Move To Review

Move a message to `Review` when it is:

* reading material that is not urgent
* not part of the approved `News` source list
* not clearly a routine notification

Examples:

* non-core newsletters
* lower-priority long-form reading
* professional updates that may still be worth a later scan

Important:

* `Review` should be used conservatively in phase 1
* if we cannot define it crisply enough, we can temporarily minimize usage until rules stabilize

### Move To Notifications

Move a message to `Notifications` when it is informational and does not need inbox presence.

Examples:

* product updates
* automated alerts
* receipts and confirmations
* routine status mail
* service notices
* system-generated updates

This folder acts as the catch-all for the broader set of low-priority informational mail.

## Safety Rules For MVP

The first production version should be conservative.

Rules:

* do not move direct human business mail out of `Inbox` unless the rule is explicit and trusted
* do not move ambiguous threads
* do not auto-delete anything
* log every move recommendation and every executed move
* keep the user in control of policy changes

Operational approach:

* start with a narrow set of high-confidence move rules
* expand only after observing good results on real traffic

## Recommended Rollout Sequence

### Phase 1

Microsoft 365 integration for:

* account linking
* read-only sync
* folder discovery / creation
* move email to `News`
* move email to `Notifications`

Keep `Review` supported in the data model and UI, but use it cautiously at first.

### Phase 2

Add:

* safer use of `Review`
* better sender/domain rules
* dashboard for move review and correction

### Phase 3

Roll the stable rules into:

* `daniel.young@digitalhealthworks.com`

## Backlog

### Account Migration

Handle mailbox transitions for:

* `boldworks.de` deprecation
* `boldworks.llc` consolidation into `danielyoung.io`

Open questions:

* whether these mailboxes stay live as managed accounts
* whether they should be treated as source mailboxes, forwarding mailboxes, or archive-only mailboxes

### Archive Remediation

Large archive folders exist and will need dedicated cleanup work later.

Backlog items:

* inventory large archive folders
* identify duplicates and low-value historical mail
* detect high-value historical threads
* define retention and cleanup rules
* decide what should remain searchable versus what should be pruned

This is intentionally not part of the first MVP.

## Immediate Next Decisions

Before implementation, define:

* the exact Microsoft mailbox to connect first
* the initial VIP sender list
* the allowlist of `News` senders/domains
* the first-pass rules for `Notifications`
* whether `Review` should be enabled immediately or held back during the first move-only rollout
