# Operations dashboard — instrumentation roadmap

This document describes how the dashboard endpoints are currently
implemented, which metrics are honestly live, and what additional
instrumentation is needed to make the "watch the platform move messages"
experience real end-to-end.

## Current contract

All endpoints require an authenticated session (the `dyc_account_email`
cookie set by the Microsoft 365 OAuth callback). They reject unauthenticated
calls with `401 No linked account session found.`

### `GET /accounts`

Lists the `connected_account` rows joined to the signed-in user. Falls back
to a synthetic `session_only` entry derived from cookies when
`DATABASE_URL` is unset.

### `GET /dashboard/summary?window_days=N`

Returns a per-user roll-up of every connected account plus per-account
panels. `window_days` is clamped to one of `1`, `7`, or `30` so the worker
queries deal with a small, predictable set of windows; the response always
echoes the resolved `window_days` and the supported buckets.

```json
{
  "generated_at": "2026-04-28T...",
  "user": {"email": "..."},
  "window_days": 7,
  "supported_window_days": [1, 7, 30],
  "totals": {
    "connected_accounts": 1,
    "mailbox_ready_accounts": 1,
    "total_folders": 22,
    "dyc_target_folders": 9,
    "messages_in": 137,
    "messages_persisted": 137,
    "messages_moved": 0,
    "errors": 0
  },
  "accounts": [
    {
      "account": {...},
      "folder_inventory": {"available": true, ...},
      "email_volume": {
        "available": true,
        "window_days": 7,
        "messages_in": 137,
        "messages_persisted": 137,
        "errors": 0,
        "last_sync_at": "2026-04-28T08:00:00+00:00",
        "last_sync_status": "success",
        "last_sync_error": null,
        "by_day": [{"day": "2026-04-22", "messages_in": 12}, ...],
        "by_folder": [{"folder": "Inbox", "messages_in": 90}, ...]
      },
      "action_activity": {
        "available": false,
        "reason": "Automated message movement is not yet implemented...",
        "messages_moved": 0
      }
    }
  ],
  "pending_instrumentation": [
    {"metric": "action_activity", "reason": "..."}
  ]
}
```

### `GET /accounts/{email}/dashboard?window_days=N`

Same per-account payload, scoped to a single account email. Returns 404
if the email does not match any of the user's connected accounts.

### `GET /activity?window_days=N`

Returns three feeds:

- `sync_activity.events` — real `sync_event` rows for `folder.bootstrap`,
  `folder.inventory.sync`, and `messages.sync` operations, with status,
  counts, and any error message. **Live.**
- `folder_activity.events` — folder bootstrap/sync events derived from
  `mailbox_folder` row timestamps. **Live.**
- `message_movement.events` — rows from `mailbox_action_event`. The table
  exists, but no code writes to it today, so this feed is `available: false`
  with an explicit `reason` until the move worker lands.

### `GET /alerts`

Computes notices from current state — never fabricated entries. Codes:

| Code | When |
|---|---|
| `runtime_config_missing` | Required runtime variable absent |
| `no_connected_accounts` | `connected_account` list is empty for this user |
| `mailbox_access_not_ready` | Linked account has no refresh token |
| `database_unavailable` | `DATABASE_URL` is unset (session-only mode) |
| `folder_inventory_missing` | Persisted account has no `mailbox_folder` rows |
| `folder_inventory_incomplete` | Inventory exists but missing default DYC folders |
| `no_message_sync_yet` | Account is set up but `messages.sync` has never run |
| `stale_message_sync` | Last `messages.sync` ran more than 24h ago |
| `recent_message_sync_error` | Most recent `messages.sync` failed |
| `no_messages_seen` | Sync succeeded but persisted zero rows |
| `move_worker_pending` | Always present until automated moves are implemented |

The Alerts tab badge in the web UI counts the entries returned here.

## Live metrics today

| Metric | Source |
|---|---|
| `connected_accounts`, `mailbox_ready_accounts` | `connected_account` |
| `account.status`, timestamps | `connected_account` |
| `folder_inventory.*` | `mailbox_folder` aggregated per account |
| `email_volume.messages_in` (window) | `message_sighting` filtered by `received_at` |
| `email_volume.messages_persisted` | `message_sighting` row count |
| `email_volume.by_day` | `date_trunc('day', received_at)` group-by |
| `email_volume.by_folder` | `folder_display_name` group-by, top 12 |
| `email_volume.last_sync_at/status/error` | latest `sync_event` for `messages.sync` |
| `email_volume.errors` | `sync_event` failure count over the window |
| `sync_activity.events` | `sync_event` rows ordered by `completed_at` |
| `action_activity.*` | `mailbox_action_event` aggregated (always zero today) |

## Schema

The runtime now lazily creates three new tables in `_ensure_account_tables`
(see `apps/api/app/main.py`); the same DDL also lives in
`migrations/0002_message_sync_instrumentation.sql` for environments that
prefer up-front migrations. The tables use the same `TEXT` primary-key /
`TEXT` `account_id` shape as `connected_account` and `mailbox_folder`, so
they coexist with the lazy-bootstrap path.

- `sync_event` — one row per sync invocation. Columns: `account_id`,
  `operation`, `status`, `folders_seen`, `messages_seen`,
  `messages_persisted`, `messages_moved`, `errors`, `error_message`,
  `detail` JSONB, `started_at`, `completed_at`.
- `message_sighting` — one row per observed message. Columns:
  `provider_message_id`, `folder_provider_id`, `folder_display_name`,
  `subject_preview` (truncated to 120 chars), `received_at`,
  `is_unread`, `has_attachments`, `first_seen_at`, `last_seen_at`. Body
  text is never fetched or stored. Subjects are limited to 120 chars to
  bound storage and reduce exposure of sensitive content.
- `mailbox_action_event` — instrumentation seam for the future move
  worker. No code writes to it today.

## Sync endpoint

### `POST /mail/messages/sync?folder_id=...&limit=...`

Pulls up to `limit` (default 50, max 200) most-recently-received messages
from Microsoft Graph for the linked account. With `folder_id` it scopes to
that mailbox folder; without it, it pulls from `/me/messages` across all
folders. For each message it persists metadata only (id, parent folder,
truncated subject, `receivedDateTime`, unread/attachment flags) into
`message_sighting`, upserting on `(account_id, provider_message_id)` so
re-runs are idempotent.

The endpoint records a `sync_event` row whether the call succeeds or
fails, so the dashboard's "last sync" tile always reflects reality.

## Operating it

To populate the dashboard with real data:

1. Sign in via the **Connect Microsoft 365** (or DHW) button on the
   Dashboard tab.
2. Open the **API / Diagnostics** tab and click **Bootstrap** to create
   the default DYC folders, then **Inventory Sync** to record the folder
   set into `mailbox_folder`. This will produce `folder.bootstrap` and
   `folder.inventory.sync` entries in the activity log.
3. Click **Sync messages** (or call `POST /mail/messages/sync` directly)
   to start collecting `message_sighting` rows. The Dashboard tab's
   *Emails seen / Persisted / Errors* tiles, the per-day sparkline, and
   the per-folder breakdown will populate immediately.
4. Re-run the sync periodically (or schedule a cron / Container Apps job
   to do so) — re-runs are idempotent and refresh `last_seen_at` /
   `last_sync_at`.

The `dyc messages-sync` CLI subcommand wraps the same endpoint for
scripting.

## Multi-account

The data model already supports multiple `connected_account` rows per
`app_user` via `(provider, provider_account_id)`, and the dashboard
endpoints iterate over every row returned by `_list_user_accounts`. The
**Connect daniel.young@digitalhealthworks.com** card on the Dashboard
sends `login_hint` to Microsoft so the picker is pre-filled.

Open follow-up: the current `/auth/microsoft/callback` overwrites the
session cookies with the most recently signed-in identity, so connecting
DHW today replaces the existing session rather than adding a sibling.
Once the OAuth callback can append rows for a different Graph `id`
without overwriting the cookie, the dashboard will pick up the second
account automatically (no UI change required).

## Remaining instrumentation

The dashboard now has live volume metrics, but two pieces are still
genuinely pending:

### 1. Automated message movement worker

Today no runtime code writes to `mailbox_action_event`. The dashboard's
**Moved** tile and `action_activity` block read zero honestly — they are
not faked. The next slice should:

- Add a worker (Container Apps Job / Functions / scheduled task) that
  scans recent `message_sighting` rows, classifies them (rules + LLM
  pass), and proposes/executes moves to the DYC-managed folders.
- Have the worker write `mailbox_action_event` rows with action_type
  `move|label|archive`, status `recommended|executed|failed`, and the
  source/target folder names. The dashboard will light up the **Moved**
  tile and the **Message movement** activity feed automatically — the
  read paths are already wired.
- Optionally promote rows to the richer `mailbox_action` table from
  `schema.md` once the workflow has approval / idempotency guarantees.

### 2. Continuous message sync (Graph delta cursor)

The current `messages.sync` endpoint pulls a bounded list of recent
messages by `receivedDateTime desc`. For full coverage and incremental
updates:

- Use Graph's `/me/messages?$delta` query with a per-folder (or
  per-mailbox) cursor stored in `connected_account.sync_cursor`.
- Schedule the sync (cron, Functions timer trigger, or Container Apps
  scaled-cron) so the dashboard always reflects the last few minutes
  rather than whatever was last clicked from API/Diagnostics.

Until the delta cursor lands, re-running `POST /mail/messages/sync`
periodically with `limit=200` provides ~best-effort coverage.

## Testing

Tests for the new endpoints live in `tests/api/test_main.py` and cover:

- Subject truncation (length cap and ellipsis behavior).
- Graph datetime parsing.
- Window-day clamping into the supported buckets.
- `_record_sync_event` no-op when `DATABASE_URL` is unset.
- `POST /mail/messages/sync` requires a session, persists sightings on
  success, and records `sync_event(status='error')` on Graph failures.
- `POST /mail/folders/bootstrap` records a `folder.bootstrap` sync_event.
- `/dashboard/summary?window_days=…` clamps and propagates the window to
  the volume loader.
- `/activity?window_days=…` includes real sync events with operation,
  status, and account metadata.
- `/alerts` flags `no_message_sync_yet` and `recent_message_sync_error`
  in the appropriate states, alongside the always-on `move_worker_pending`.
- The `dyc messages-sync` CLI subparser parses correctly.

The protected-endpoints sweep
(`test_protected_mailbox_endpoints_reject_unauthenticated_calls`) keeps
`/activity`, `/alerts`, and dashboard URLs gated.
