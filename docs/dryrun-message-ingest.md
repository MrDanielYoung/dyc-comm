# Dry-run message ingest + classification

This slice adds the first concrete, non-destructive read of a real
mailbox into DYC Comm. It pulls a small batch of recent messages from
Microsoft Graph for the signed-in account, persists minimal metadata,
runs the deterministic classifier from
[`docs/ai-classifier-policy.md`](ai-classifier-policy.md), and writes a
classification recommendation per message. It does **not** move, label,
send, delete, or change folders.

The primary evaluation mailbox is **daniel@danielyoung.io** — the
inbox is intentionally noisy and is the target we use to assess the
classifier's usefulness. All examples below assume that account.

## What the slice provides

- `POST /mail/messages/ingest-dry-run?limit=N&email=...` — operator
  endpoint that triggers one read-only ingest run and returns the
  per-message classification decisions.
- `GET /mail/messages/recommendations?limit=N` — the dashboard log of
  recent recommendations and runs for the signed-in account.
- `GET /activity` now includes a `classify_activity` block with one
  event per recommendation (subject/sender/date, recommended folder,
  confidence, forced_review, reasons, safety flags, status, error).
- `GET /dashboard/summary` now includes a per-account `dryrun_classify`
  summary with run / message / forced-review / error counts.
- `dyc ingest-dry-run` and `dyc recommendations` CLI subcommands.

## Safety guarantees

- The Graph call is `GET /me/messages` only. The dry-run path does not
  invoke `_graph_post`, `PATCH`, or `DELETE` against any Graph URL.
- The classifier runs with `provider_consulted=False` whenever the
  Azure OpenAI / Azure AI provider env vars are not configured. The
  run still completes and the recommendation is logged with
  `provider: "none"`. Operators can validate the slice with no
  AI-provider credentials.
- Every recommendation that is uncertain, sensitive, legal, judgment-
  required, short, or thread-flipped is forced to `10 - Review` per
  the existing classifier contract.
- Operator routes never let one signed-in user trigger ingest against
  an unrelated mailbox: the optional `email` query parameter must
  match the current session email.

## Manual validation — daniel@danielyoung.io

These are the exact steps to evaluate the slice end-to-end in a real
environment. They assume the API is deployed and reachable at the URL
in `API_BASE_URL` (production:
`https://api.comm.danielyoung.io`).

### 1. Sign in as daniel@danielyoung.io

1. Open the web app (`https://comm.danielyoung.io` in production).
2. Click **Sign in with Microsoft** and complete the OAuth flow as
   `daniel@danielyoung.io`. Confirm `mailbox_access_ready: true` on
   the Dashboard tab.

> The account email must be allow-listed via
> `ALLOWED_ACCOUNT_EMAILS` and the tenant via
> `ALLOWED_MICROSOFT_TENANT_IDS` (or
> `MICROSOFT_ENTRA_TENANT_ID` for single-tenant mode). See
> [`docs/web-access-control.md`](web-access-control.md).

### 2. Confirm folder inventory

The classifier returns canonical folder names like `10 - Review`,
`20 - News`, etc. Bootstrap them once if they are not already present:

```bash
dyc bootstrap
dyc inventory-sync
```

Verify on the Dashboard that `folder_inventory.is_bootstrapped` is
`true`. The dry-run ingest does not require bootstrap to succeed —
folders are not modified by ingest — but having canonical folders
present makes the recommended-folder values meaningful.

### 3. Run a small dry-run ingest

The default limit is **10 messages**, the maximum is **50**. Start
small to keep the noise manageable:

```bash
dyc ingest-dry-run --limit 10 --email daniel@danielyoung.io
```

The CLI prints the JSON body, including:

- `dry_run: true`, `non_destructive: true`
- `account.email == "daniel@danielyoung.io"`
- `totals.fetched`, `totals.classified`, `totals.forced_review`,
  `totals.errors`
- `provider.selected`, `provider.configured`, `provider.consulted`
  (the last is always `false` in this slice)
- one item per message under `items[]` with the full
  `recommendation` object: subject, sender, received_at, recommended
  folder, confidence, confidence band, forced_review, reasons,
  safety_flags

Every recommended_folder you see should either be one of the
DYC-managed folders or `10 - Review`. Anything else is a bug.

### 4. Verify the dashboard log

```bash
dyc recommendations --limit 25
```

The response includes:

- `recommendations[]` — the most recent classifications across all
  runs for `daniel@danielyoung.io`, in reverse chronological order.
- `recent_runs[]` — the audit log of runs (started_at, finished_at,
  fetched_count, classified_count, forced_review_count,
  error_count, status, error).

In the web UI, the Activity Log tab now shows a `classify_activity`
section with one row per recommendation. The Dashboard tab's
per-account panel shows the `dryrun_classify` rollup
(`messages_seen`, `recommendations`, `forced_review`, `errors`,
`last_run_at`).

### 5. Sanity-check the safety flags

The inbox for `daniel@danielyoung.io` is noisy on purpose. Skim the
log for messages where `forced_review` is true and confirm the
`reasons` / `safety_flags` make sense:

- `short_without_context` for terse pings.
- `sensitive_content` for anything that mentions patient/clinical
  language (e.g. BIOTRONIK-related threads).
- `legal_or_contractual` for contracts/agreements/NDAs.
- `judgment_required` for tone/politics/obligation language.
- `confidence below medium threshold — defaulting to 10 - Review`
  when no rule signal fired.

If a message you would have routed to `20 - News` (etc.) is being
forced to `10 - Review`, that is the expected behaviour for now —
the deterministic rule layer that lifts confidence above the medium
threshold for known senders is not part of this slice. Note the
sender so we can extend the rule layer in a follow-up.

### 6. Confirm read-only behaviour in the mailbox

In Outlook on the web (or the desktop client), open
`daniel@danielyoung.io` and confirm:

- Messages have not been moved out of `Inbox`.
- No new folders have been created.
- No items appear in `Sent Items`, `Drafts`, or `Deleted Items` as a
  result of the dry-run.
- The unread state of messages is unchanged.

## Direct HTTP usage

The CLI is a thin wrapper around the API. Equivalent direct calls,
assuming a session cookie file from sign-in:

```bash
# Trigger a 10-message dry-run for the signed-in account.
curl -sS -X POST \
  -b cookies.txt -c cookies.txt \
  "$API_BASE_URL/mail/messages/ingest-dry-run?limit=10&email=daniel@danielyoung.io" \
  | jq

# List recent recommendations.
curl -sS \
  -b cookies.txt \
  "$API_BASE_URL/mail/messages/recommendations?limit=25" \
  | jq
```

## What this slice does NOT do

- It does not run on a schedule. Each run is operator-triggered.
- It does not de-duplicate against earlier runs at the recommendation
  level — re-running ingest writes a new recommendation row per
  message even if the metadata is unchanged. This is intentional: the
  dashboard shows a history of how the same message was classified
  across runs.
- It does not call any LLM. The classifier is deterministic. When
  Azure OpenAI / Azure AI credentials are present they are recorded
  on the run row but `provider_consulted` is still `false`.
- It does not move, label, send, or delete any mail. Those actions
  remain gated on the human-in-the-loop flow defined in
  [`architecture.md`](../architecture.md) §9.

## Storage

Three new tables are bootstrapped lazily by the API on first use:

- `email_message_dryrun` — minimal per-message metadata
  (subject, sender, received_at, parent folder, web link, body
  preview). Upserted by `(account_id, provider_message_id)`.
- `message_classification_recommendation` — one row per
  classification attempt, with the full decision contract plus a
  `status` of `recommended` or `error` and an `error` string.
- `dryrun_ingest_run` — audit row per operator-triggered run,
  with totals and the start/finish timestamps used by the dashboard
  rollup.

All three tables live alongside `connected_account` and
`mailbox_folder` and use the same on-demand bootstrap pattern. The
canonical schema in `migrations/0001_initial.sql` will be extended in
a follow-up to incorporate them.
