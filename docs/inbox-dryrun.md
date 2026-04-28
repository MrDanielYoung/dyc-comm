# Inbox dry-run classification

This slice fetches a small batch of recent inbox messages from a connected
Microsoft 365 account, runs them through the existing dry-run classifier
([apps/api/app/classifier.py](../apps/api/app/classifier.py),
[ai-classifier-policy.md](./ai-classifier-policy.md)), and persists the
recommendation so an operator can review what *would* happen before any
mailbox-mutating behavior is built.

It is **strictly non-destructive**:

- Microsoft Graph is only consulted via `GET /me/mailFolders/inbox/messages`
  with a small `$top` and `$select`. No `POST`, `PATCH`, or `DELETE` is
  ever issued from this code path.
- Nothing is moved, sent, or deleted. The mailbox folder layout is not
  touched.
- Only the metadata we will need for review is stored: provider message
  id, account email, received date, sender, subject, current folder id,
  classification recommendation, confidence + band, `forced_review`,
  reasons, safety flags, whether the AI provider was consulted, and any
  per-message error.
- `10 - Review` is the fallback recommendation for any unclear,
  sensitive, or low-confidence message.
- When `AZURE_OPENAI_*` / `AZURE_AI_*` env vars are absent the
  deterministic classifier runs and the response is marked
  `provider_consulted: false` (the per-row `provider` column is `none`).

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/mail/inbox/classify-dryrun?account=<email>&limit=<n>` | Fetch up to `limit` recent inbox messages for `account` and persist a dry-run classification per message. |
| `GET`  | `/mail/inbox/classify-dryrun/log?account=<email>&limit=<n>` | Read back the persisted dry-run rows for `account`, ordered by received date desc. |

Both endpoints require an authenticated session cookie
(`dyc_account_email`). The `account` query parameter must match an account
already linked to that session — otherwise the API returns `404`.

`limit` accepts `1..100`; default is `25`.

## CLI

```sh
# Run the dry-run scan against the default operator account.
python -m apps.api.app.cli inbox-dryrun --account daniel@danielyoung.io --limit 25

# Read the persisted log back, scoped to the same account.
python -m apps.api.app.cli inbox-dryrun-log --account daniel@danielyoung.io --limit 25
```

The CLI persists cookies under `.dyc/cookies.json` (override with
`--cookie-file`). Both subcommands default `--account` to
`daniel@danielyoung.io` so an operator can run them without flags during
the bring-up.

## Web UI (Inbox Sorting tab)

Once signed in, the dashboard exposes the same flow without leaving the
browser:

1. The **Dashboard** tab includes an **Inbox sorting (dry-run)** card
   that summarises the most recent persisted dry-run for the selected
   account and links to the full **Inbox Sorting** tab.
2. The **Inbox Sorting** tab provides:
   - a **Run inbox classification (dry-run)** button that calls
     `POST /mail/inbox/classify-dryrun?account=<selected>&limit=<n>`,
   - a **Refresh log** button that calls
     `GET /mail/inbox/classify-dryrun/log`,
   - a read-only banner stating that no messages are moved, sent, or
     deleted, and unclear/low-confidence messages route to
     `10 - Review`,
   - a results table with one row per message showing the recommended
     folder, category, confidence (with band), reasons, safety flags,
     `forced_review` status, and any per-message error.
3. Acting on a recommendation (moving the message into the suggested
   folder) is **not** wired up in this build — that requires
   per-message confirmation and an audit-logged write path. The UI
   makes this explicit (`0 messages moved` is shown in the summary).

The account selector in the left rail scopes every panel — including the
Inbox Sorting tab — to the chosen connected account. The default is
`daniel@danielyoung.io` for the bring-up.

## Manual validation steps (operator)

These are the exact steps a human operator should run with
`daniel@danielyoung.io` connected:

1. **Sign in.** From the web app, click **Connect Microsoft 365** and
   complete sign-in as `daniel@danielyoung.io`. Confirm the dashboard
   shows the linked account and `mailbox_access_ready: true`.
2. **(Optional) Bootstrap folders** so `10 - Review` exists in the
   mailbox to receive any future moves:
   ```sh
   python -m apps.api.app.cli bootstrap
   ```
3. **Run the dry-run scan**:
   ```sh
   python -m apps.api.app.cli inbox-dryrun --account daniel@danielyoung.io --limit 25
   ```
   Expected: a JSON payload with `dry_run: true`, `destructive: false`,
   `fetched <= 25`, a `results[]` array containing one
   `recommendation` per message (`recommended_folder` always set,
   `forced_review: true` for unclear/sensitive/low-confidence messages),
   and `provider.consulted: false` if Azure OpenAI/AI is not configured.
4. **Read the log back**:
   ```sh
   python -m apps.api.app.cli inbox-dryrun-log --account daniel@danielyoung.io --limit 25
   ```
   Expected: the rows just persisted, sorted by received date desc.
5. **Verify the mailbox is unchanged.** In Outlook, confirm:
   - No messages have moved out of `Inbox`.
   - No new mail folders were created (other than any you bootstrapped
     in step 2).
   - No items appear in `Sent Items`, `Drafts`, or `Deleted Items`
     because of the scan.
6. **Re-run the dry-run.** It is idempotent — re-classifying the same
   message updates the existing row (`UNIQUE(account_id,
   provider_message_id)` upserts).

## Troubleshooting

- `401 No linked account session found.` — sign in via the web app first
  so the `dyc_account_email` cookie is set.
- `404 No connected account with that email is linked to this session.`
  — the `account` query parameter must match an account this session
  actually linked. Check `/accounts` first.
- `409 Database-backed mailbox access is not configured.` — `DATABASE_URL`
  must be set; without it the mailbox-credentials lookup cannot run.
- Empty `results: []` — the inbox may be empty, or the connected account
  may have rotated tokens; re-run Microsoft sign-in.
