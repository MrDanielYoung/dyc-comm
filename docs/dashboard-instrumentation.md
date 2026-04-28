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

## Activity log (`/dashboard/activity`, `/accounts/{email}/activity`)

The activity log reads from `audit_event`. Today the API writes three
event types:

| Event type             | Emitted by                         |
|------------------------|------------------------------------|
| `account.linked`       | `/auth/microsoft/callback`         |
| `folder.bootstrap`     | `/mail/folders/bootstrap`          |
| `folder.inventory_sync`| `/mail/folders/inventory/sync`     |

These events are honest signals — they only fire when the corresponding
operation actually runs against Microsoft Graph and/or the database.

The endpoint response includes an `instrumentation` block:

```json
"instrumentation": {
  "available": true,
  "covers": ["account.linked", "folder.bootstrap", "folder.inventory_sync"],
  "message_movement_available": false,
  "reason": "Message-level movement events ... not yet emitted ..."
}
```

`message_movement_available` flips to `true` once a connector worker
starts writing message-level events (e.g. `message.moved`,
`mailbox_action.executed`, `mailbox_action.failed`). Until then, the
activity tab in the UI shows a "pending" banner with the reason text and
hides any movement-specific affordances.

### What still needs to land for full message movement

1. Connector worker writes `email_message` rows on Microsoft Graph delta
   changes.
2. Action executor writes `mailbox_action` rows and emits
   `audit_event(event_type='mailbox_action.executed' / '...failed',
   mailbox_action_id=...)` on every transition.
3. Folder-move helper emits `event_type='message.moved'` with
   `before_state/after_state` JSON capturing the source and destination
   folder IDs.

The `_load_account_activity` query already joins by `account_id` and
returns `metadata` JSON, so once the new event types are emitted they
appear in the existing UI without further changes.

## Alerts (`/dashboard/alerts`)

`/dashboard/alerts` returns a sorted list of attention items derived from
real state. There is no fabrication: every alert points at an env var, a
`connected_account` row, a `mailbox_folder` summary, or an
`audit_event` query result.

Alert IDs and severities currently emitted:

| ID prefix                              | Severity | Source |
|----------------------------------------|----------|--------|
| `runtime_var_missing:<NAME>`           | error / warning | `os.getenv` |
| `no_persisted_account`                 | warning  | `_list_user_accounts` returns nothing |
| `account_no_refresh:<email>`           | error    | `connected_account.refresh_token IS NULL` |
| `folders_empty:<email>`                | warning  | `_summarize_folder_inventory.total_folders == 0` |
| `folders_not_bootstrapped:<email>`     | warning  | dyc target count below `expected_dyc_target_count` |
| `no_activity:<email>`                  | info     | no `audit_event` rows for the account |
| `stale_activity:<email>`               | warning  | last `audit_event` older than 7 days |
| `pending_instrumentation:email_volume` | info     | always emitted until the connector worker lands |
| `pending_instrumentation:action_activity` | info  | always emitted until the action executor lands |

`counts` returns `{error, warning, info}` totals so the UI can show a
count badge on the Alerts tab.

## Multi-account (DHW account)

The data model already supports multiple `connected_account` rows per
`app_user` via `(provider, provider_account_id)`. The OAuth flow now
accepts an optional `login_hint` (or `target_email` alias) query
parameter on `/auth/microsoft/start` so the Microsoft consent screen can
pre-fill a specific mailbox without changing the URL of the existing
sign-in.

The web Dashboard tab includes an "Add another Microsoft mailbox" CTA
with a one-click link for `daniel.young@digitalhealthworks.com`. The CTA
forwards the user to:

```
/auth/microsoft/start?login_hint=daniel.young@digitalhealthworks.com
```

### Connecting the DHW account, step by step

1. Sign in as the existing operator account first (the session cookie
   remains required to gate the console).
2. From the Dashboard tab, click **Connect daniel.young@digitalhealthworks.com**.
3. Microsoft prompts for that exact account; the user authorizes the
   same scopes (`Mail.Read Mail.ReadWrite User.Read offline_access`).
4. The OAuth callback persists a *second* `connected_account` row keyed
   on the new Graph `id`. The cookie session updates to that account
   (current single-cookie behaviour).
5. Run **Bootstrap** and **Inventory Sync** under API/Diagnostics. Those
   actions emit `folder.bootstrap` and `folder.inventory_sync` audit
   events for the DHW account, which immediately appear on the Activity
   Log tab.
6. The dashboard summary, alerts, and activity endpoints automatically
   reflect both accounts because they iterate `_list_user_accounts`.

### Known limitation

The current callback still sets a single `dyc_account_email` cookie, so
the *active session view* points at whichever mailbox most recently
finished sign-in. `/accounts` and `/dashboard/summary` already iterate
both rows; the next slice should add a `linked_account_id` cookie or a
UI picker so the Diagnostics tab can drill into a specific mailbox
without re-authenticating. Until then, sign in as the account you want
to inspect closely.

## Next instrumentation slice (after this PR)

The most leveraged next step is the connector worker that writes
`email_message` rows from Microsoft Graph delta queries. That single
slice unlocks:

- `email_volume.messages_in` daily series (group by
  `date_trunc('day', received_at)`).
- The `messages_in` / `processed` / `errors` charts on the dashboard.
- Real `message.moved` audit events on the Activity Log tab.
- `stale_activity` alerts that are meaningful (since events would be
  flowing continuously rather than only on manual sync).

## Testing

Tests for the new endpoints live in `tests/api/test_main.py` and cover:

- Unauthenticated access returns `401` (sweep includes
  `/dashboard/activity`, `/dashboard/alerts`, and
  `/accounts/{email}/activity`).
- Session-only fallback when no DB persistence is configured for
  `/accounts`, `/dashboard/summary`, and `/dashboard/activity`.
- Aggregation of folder inventory when persisted rows are returned.
- 404 for `/accounts/{email}/dashboard` and
  `/accounts/{email}/activity` when the email is not linked.
- `/dashboard/activity` returns events ordered newest first and tags
  `message_movement_available: false` until the connector worker lands.
- `/dashboard/alerts` emits `runtime_var_missing` errors when env vars
  are absent, `no_persisted_account` when the user has no
  `connected_account` row, `account_no_refresh` /
  `folders_empty` / `no_activity` for partial setups, and the always-on
  `pending_instrumentation:*` info alerts.
- `/auth/microsoft/start` forwards a valid `login_hint` (or the
  `target_email` alias) query parameter to the Microsoft authorize URL,
  and silently drops malformed values.
