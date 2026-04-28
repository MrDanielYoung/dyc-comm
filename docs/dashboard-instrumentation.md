# Operations dashboard — instrumentation roadmap

This document describes how the `/dashboard/summary`,
`/accounts/{email}/dashboard`, and `/accounts` endpoints are currently
implemented, which metrics are honestly live, and what additional
instrumentation is needed to make the "watch the platform move messages"
experience real.

## Current contract

All endpoints require an authenticated session (the `dyc_account_email`
cookie set by the Microsoft 365 OAuth callback). They reject unauthenticated
calls with `401 No linked account session found.`

### `GET /accounts`

Lists the `connected_account` rows joined to the signed-in user. If
`DATABASE_URL` is unset (e.g. in local dev without a database) the endpoint
falls back to a synthetic `session_only` entry derived from the cookies, so
the UI can still render an honest "linked but not persisted" state instead
of fabricating data.

### `GET /dashboard/summary`

Returns a per-user roll-up of every connected account plus per-account
panels. The shape is intentionally explicit about which metrics are live
and which are pending:

```json
{
  "generated_at": "2026-04-28T...",
  "user": {"email": "..."},
  "totals": {
    "connected_accounts": 1,
    "mailbox_ready_accounts": 1,
    "total_folders": 22,
    "dyc_target_folders": 9
  },
  "accounts": [
    {
      "account": {
        "account_id": "...",
        "provider": "microsoft_365",
        "email": "...",
        "display_name": "...",
        "status": "active",
        "mailbox_access_ready": true,
        "token_updated_at": "...",
        "created_at": "...",
        "updated_at": "..."
      },
      "folder_inventory": {
        "available": true,
        "total_folders": 22,
        "dyc_target_folders": 9,
        "expected_dyc_target_count": 9,
        "is_bootstrapped": true,
        "by_ownership": {"dyc_managed": 9, "system": 7, "legacy_rule": 4, "manual": 2}
      },
      "email_volume": {"available": false, "reason": "...", "messages_in": null, ...},
      "action_activity": {"available": false, "reason": "...", "actions_executed": null, ...}
    }
  ],
  "pending_instrumentation": [
    {"metric": "email_volume", "reason": "..."},
    {"metric": "action_activity", "reason": "..."}
  ]
}
```

### `GET /accounts/{email}/dashboard`

Same per-account payload, scoped to a single account email. Returns 404 if
the email does not match any of the user's connected accounts.

## Live metrics today

These come straight out of tables that are written to during the OAuth
callback and the folder-inventory sync flow:

| Metric | Source |
|---|---|
| `connected_accounts` | `connected_account` rows joined on the signed-in `app_user` |
| `mailbox_ready_accounts` | `connected_account.refresh_token IS NOT NULL` |
| `account.status` | `connected_account.status` |
| `account.token_updated_at` / `account.updated_at` / `account.created_at` | `connected_account` timestamps |
| `folder_inventory.*` | `mailbox_folder` rows aggregated per account, plus the `DEFAULT_MVP_FOLDER_SPECS` constant for the expected target count |
| `dryrun_classify.*` | Aggregated from `dryrun_ingest_run` rows for the account; reports `available: false` until the first operator-triggered run |
| `classify_activity` events on `/activity` | One event per row in `message_classification_recommendation`, joined to the source message metadata in `email_message_dryrun` |

## Pending instrumentation

The two metric blocks below are deliberately returned as
`available: false` with a `reason`, and the UI tags them as
`pending`. Do not start fabricating values here — populate them only
after the supporting pipeline is in place.

### 1. `email_volume` — messages in / processed / errors over time

Required schema work (already documented in `schema.md`, not yet in the
runtime bootstrap path):

- `email_message` — populate `received_at`, `account_id`, ingestion
  timestamps.
- `email_thread` — populate `last_message_at`, `last_ingested_at`.
- A new `message_ingestion_event` (or reuse `audit_event` with
  `event_type='ingest.success' / 'ingest.failure'`) for hard error counts
  beyond the soft state already in `sync_job`.

Required runtime work:

- A connector worker that pulls `/me/messages?$delta` from Microsoft Graph
  on a schedule, normalizes into `email_message`, and bumps a sync cursor
  in `sync_job`.
- A query in `_dashboard_summary` like
  `SELECT date_trunc('day', received_at), count(*) FROM email_message
  WHERE account_id = %s AND received_at >= now() - interval '7 days'
  GROUP BY 1 ORDER BY 1`.
- Mirror counts for "processed" by joining `email_message` against
  `thread_classification` on the latest `is_current` row.

When the queries are wired, swap `_empty_volume_metrics()` for a real
loader that returns the same shape but with `available: true`.

### 2. `action_activity` — recommended / executed / failed actions

Required schema work:

- `mailbox_action` already exists in `schema.md`, but the runtime
  `_ensure_account_tables` bootstrap does not create it; either run
  `migrations/0001_initial.sql` against the production database, or extend
  the bootstrap to create the subset the dashboard needs.
- `audit_event` should record execution and failure events tied to a
  `mailbox_action_id`.

Required runtime work:

- Emit `audit_event` rows from the eventual action-execution worker
  (currently the API only persists folder inventory; no actions are
  executed yet).
- Add a query that counts `mailbox_action.status` per account over the
  trailing window plus `MAX(executed_at)` for "last action".

## Activity log and alerts (this slice)

The web UI now ships four tabs: **Dashboard**, **Activity Log**, **Alerts**,
and **API / Diagnostics**. Two new authenticated endpoints back the new tabs:

### `GET /activity`

Returns folder bootstrap / inventory-sync events derived from `mailbox_folder`
rows (the only actions the runtime currently performs). Per-message movement
events are returned as `available: false` with an explicit `reason`, plus a
`pending_instrumentation` block, until the connector worker writes
`message_ingestion_event` / `mailbox_action` rows. The folder feed
distinguishes `folder.bootstrap` (created in this run) from `folder.sync`
(updated later) using the row's `created_at` vs `updated_at` timestamps.

### `GET /alerts`

Computes notices from current state — never fabricated or example items.
Each entry has a stable `code`, a `severity` (`error` / `warning` / `info`),
a human-readable `message`, and a `next_action`. Codes currently emitted:

| Code | When |
|---|---|
| `runtime_config_missing` | Any required runtime variable is absent |
| `no_connected_accounts` | `connected_account` list is empty for this user |
| `mailbox_access_not_ready` | Linked account has no refresh token |
| `database_unavailable` | `DATABASE_URL` is unset (session-only mode) |
| `folder_inventory_missing` | Persisted account has no `mailbox_folder` rows |
| `folder_inventory_incomplete` | Inventory exists but is missing default DYC folders |
| `activity_instrumentation_pending` | Always present until message-movement instrumentation lands |

The Alerts tab badge in the web UI counts the entries returned here.

## Multi-account next slice (DHW account)

The data model already supports multiple `connected_account` rows per
`app_user` via `(provider, provider_account_id)`. The current OAuth flow,
however, is keyed on the cookie email alone, so re-running
`/auth/microsoft/start` with a second Microsoft account will replace the
session rather than add a sibling account.

To honestly add the DHW account:

1. Click **Connect daniel.young@digitalhealthworks.com** on the Dashboard
   tab. The web UI sends `login_hint=daniel.young@digitalhealthworks.com`
   to `GET /auth/microsoft/start`, which forwards it to Microsoft's
   `/oauth2/v2.0/authorize` so the picker is pre-filled. The existing
   `prompt=select_account` is preserved, so a different account can still
   be picked.
2. Decide whether `app_user` is "the platform operator" (one row) and DHW
   is a second `connected_account` under the same user, or whether the DHW
   mailbox should belong to its own `app_user`. **Open follow-up:** the
   current `microsoft_callback` overwrites the session cookies with the
   most recently signed-in identity, so connecting DHW today replaces the
   existing session rather than adding a sibling account. Until that's
   fixed, DHW connect is exercised one account at a time.
3. Update `_persist_microsoft_account` so a second OAuth login under a
   different Graph `id` upserts a new `connected_account` row instead of
   replacing the existing session cookie's view.
4. Add an account picker (or a `linked_account_id` cookie) to the web UI
   so per-account dashboards can be selected.
5. Re-run the folder inventory sync against the DHW account.

The `/accounts` endpoint already returns every row, so the dashboard UI
will pick up the second account automatically once the persistence side
supports it.

## Testing

Tests for the new endpoints live in `tests/api/test_main.py` and cover:

- Unauthenticated access returns `401`.
- Session-only fallback when no DB persistence is configured.
- Aggregation of folder inventory when persisted rows are returned.
- 404 for `/accounts/{email}/dashboard` when the email is not linked.
- `/auth/microsoft/start` omits `login_hint` by default and forwards the
  query value when one is supplied.
- `/activity` returns an empty available-flagged feed when no accounts are
  persisted, and surfaces folder events with `event_type` and `account`
  metadata when activity is loaded.
- `/alerts` flags `runtime_config_missing`, `no_connected_accounts`,
  `mailbox_access_not_ready`, `database_unavailable`,
  `folder_inventory_missing`, and the always-on
  `activity_instrumentation_pending` entry.

The protected-endpoints sweep in `test_protected_mailbox_endpoints_reject_unauthenticated_calls`
includes `/activity` and `/alerts` alongside the three dashboard URLs.
