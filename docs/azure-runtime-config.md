# Azure Runtime Configuration

This document describes the runtime configuration the DYC Comm API and web
deployments expect in Azure Container Apps, and the repo-native helper used
to apply API settings without ad-hoc portal edits.

For local development, see [`docs/local-setup.md`](local-setup.md). For the
helper script reference, see [`infra/azure/README.md`](../infra/azure/README.md).

## Container Apps

Current target apps (production):

- `dyc-comm-prod-api` — FastAPI service container
- `dyc-comm-prod-web` — static web shell (Nginx)

Shared Container Apps environment:

- `dyc-comm-prod-cenv`

## Web app

The web shell currently served by `dyc-comm-prod-web` is static HTML from
Nginx (see `apps/web/`).

Runtime expectations:

- public ingress enabled
- target port `80`
- no required runtime secrets

## API app

Endpoints currently exposed by `apps/api/app/main.py` on `main`:

- `GET /health`
- `GET /config-check`
- `GET /auth/session`
- `GET /auth/microsoft/start`
- `GET /auth/microsoft/callback`
- `POST /auth/logout`
- `GET /mail/folders` — live Microsoft Graph fetch, requires a linked
  account session and DB-backed tokens.

> The Microsoft Entra OAuth flow is wired up. DB-backed mailbox folder
> bootstrap/sync/inventory endpoints remain a follow-up slice.

### Required environment variables

These mirror `apps/api/.env.example` and `infra/azure/api-runtime-settings.env.example`:

| Variable | Purpose |
| --- | --- |
| `APP_ENV` | `production` for the deployed Container App. |
| `DATABASE_URL` | PostgreSQL connection string. Treat as a secret. |
| `MICROSOFT_ENTRA_CLIENT_ID` | Entra app registration client id. |
| `MICROSOFT_ENTRA_TENANT_ID` | Entra tenant id. |
| `MICROSOFT_ENTRA_CLIENT_SECRET` | Entra client secret. Treat as a secret. |
| `MICROSOFT_ENTRA_REDIRECT_URI` | OAuth callback URL the API serves. |
| `WEB_APP_URL` | Public origin of the web shell; OAuth callback redirects here. |
| `API_BASE_URL` | Public origin of the API; reported by `/config-check`. |
| `ALLOWED_ORIGINS` | Optional comma-separated CORS allowlist. Defaults to `WEB_APP_URL`. |
| `KEY_VAULT_REFS_ENABLED` | `true` when secrets resolve via Container App Key Vault refs; `false` when the workflow copies values from Key Vault into plain Container App env vars. |
| `ALLOWED_MICROSOFT_TENANT_IDS` | Comma-separated tenant id allow-list enforced by the OAuth callback. Built at apply time from the committed Decoding Options seed plus partner tenant ids fetched from Key Vault (`dhw-tid`, `bw-tid`). When unset on a container, the API falls back to `MICROSOFT_ENTRA_TENANT_ID` only. |
| `ALLOWED_ACCOUNT_EMAILS` | Comma-separated per-user email allow-list enforced by the OAuth callback. **Required** alongside `ALLOWED_MICROSOFT_TENANT_IDS`: a tenant allow-list alone would admit any user in DHW/BW. Sourced from Key Vault secret `allowed-account-emails`. |
| `MOTION_TASKS_ENABLED` | Enables one-way Motion task creation for highly important emails. |
| `MOTION_API_KEY` | Motion API key. Treat as a secret. |
| `MOTION_WORKSPACE_ID` | Optional Motion workspace id. If unset, the API uses Motion's first returned workspace. |
| `MOTION_ASSIGNEE_ID` | Optional Motion user id to assign tasks to. If unset, the API assigns tasks to the current API user. |

The Entra values are seeded now so the API container can boot with the same
configuration shape across environments and so `/config-check` can report
whether secrets are wired up. Wiring the actual auth flow is a separate,
future change.

### Ingress

- public ingress enabled
- target port matches the container's listen port (`80` for the current
  Nginx-fronted image)

## Key Vault reference approach

Steady-state target:

- Non-secret values (`APP_ENV`, `KEY_VAULT_REFS_ENABLED`, redirect URIs,
  client and tenant ids) are stored as plain Container App environment
  variables.
- Secret values (`DATABASE_URL`, `MICROSOFT_ENTRA_CLIENT_SECRET`) are
  stored in Azure Key Vault and referenced from the Container App as
  Key Vault secret refs, so secret material never lives in the repo or in
  the Container App configuration directly.
- `KEY_VAULT_REFS_ENABLED=true` is set in environments where the API
  should treat secrets as Key Vault-backed; `/config-check` reflects this.

Acceptable interim state, until each secret has a Key Vault entry and a
matching Container App ref:

- Set the value directly on the Container App as a plain environment
  variable, with `KEY_VAULT_REFS_ENABLED=false`.
- Track the conversion to a Key Vault ref as follow-up work; do not leave
  a secret as a plain env var long-term.

## Applying API runtime settings

The repo ships a small helper for this so production updates are explicit
and reviewable instead of being clicked through the Azure portal.

Files:

- [`infra/azure/api-runtime-settings.env.example`](../infra/azure/api-runtime-settings.env.example)
  — template listing the variables the API container expects. Copy to a
  private, gitignored file (for example `infra/azure/api-runtime-settings.env`)
  and fill in real values.
- [`infra/azure/apply-api-settings.sh`](../infra/azure/apply-api-settings.sh)
  — wraps `az containerapp update --set-env-vars` and reads `KEY=value`
  lines from the env file.

Usage:

```bash
# 1. Copy the template and fill in real values locally. Do NOT commit it.
cp infra/azure/api-runtime-settings.env.example infra/azure/api-runtime-settings.env

# 2. Apply to the default Container App (dyc-comm-prod-api in dyc-comm-prod-rg).
./infra/azure/apply-api-settings.sh infra/azure/api-runtime-settings.env

# 3. Override the target app / resource group via env vars when needed.
RESOURCE_GROUP=my-rg CONTAINER_APP_NAME=my-api \
  ./infra/azure/apply-api-settings.sh infra/azure/api-runtime-settings.env
```

Defaults:

| Variable             | Default              |
| -------------------- | -------------------- |
| `RESOURCE_GROUP`     | `dyc-comm-prod-rg`   |
| `CONTAINER_APP_NAME` | `dyc-comm-prod-api`  |

Prerequisites: Azure CLI authenticated against the right subscription
(`az login`), permission to update the target Container App, and Bash 4+.

## Applying runtime settings via GitHub Actions

For runs that should be auditable in CI rather than executed from a
laptop, the repo also ships a manual-only workflow:

- [`.github/workflows/configure-api-runtime.yml`](../.github/workflows/configure-api-runtime.yml)
  — `workflow_dispatch`-only. Runs in the `production` GitHub Environment,
  authenticates to Azure via OIDC (`azure/login@v2`), then runs a single
  Azure CLI step that reads the runtime secrets from Azure Key Vault
  (`dyc-comm-prod-kv`) into local shell variables and issues
  `az containerapp update --set-env-vars` against
  `dyc-comm-prod-api`/`dyc-comm-prod-rg` with the variable shape
  documented above. Secret values stay in-memory inside that single
  step and are never written to `$GITHUB_ENV` or otherwise exported
  across steps.

Key Vault is the source of truth for secret values. The workflow does
not require duplicate GitHub secrets for `DATABASE_URL` or any
`MICROSOFT_ENTRA_*` value.

Required GitHub secrets (configure on the `production` environment, not
as plain repo secrets):

| Secret | Source |
| --- | --- |
| `AZURE_CLIENT_ID` | OIDC federated credential for the deploy app registration. |
| `AZURE_TENANT_ID` | Entra tenant id. |
| `AZURE_SUBSCRIPTION_ID` | Target Azure subscription. |

Required Key Vault secrets in `dyc-comm-prod-kv` (must be enabled and
non-empty):

| Container App env var | Key Vault secret name |
| --- | --- |
| `DATABASE_URL` | `PGdb-URL` |
| `MICROSOFT_ENTRA_CLIENT_ID` | `microsoft-entra-client-id` |
| `MICROSOFT_ENTRA_TENANT_ID` | `microsoft-entra-tenant-id` |
| `MICROSOFT_ENTRA_CLIENT_SECRET` | `dyc-comm-prod-value` |
| `ALLOWED_MICROSOFT_TENANT_IDS` (DHW partner tenant id) | `dhw-tid` |
| `ALLOWED_MICROSOFT_TENANT_IDS` (BW partner tenant id) | `bw-tid` |
| `ALLOWED_ACCOUNT_EMAILS` | `allowed-account-emails` |
| `MOTION_API_KEY` | `motion-api-key` |

`ALLOWED_MICROSOFT_TENANT_IDS` is built at apply time as the comma-separated
concatenation of the Decoding Options tenant id
(`99c0f350-71bd-47f9-ab6a-cf10bc76533a`, committed in the workflow as
`ALLOWED_MICROSOFT_TENANT_IDS_SEED`) plus the values fetched from Key
Vault secrets `dhw-tid` and `bw-tid`. Partner tenant ids stay in Key
Vault rather than in the repo, while the Decoding Options tenant id —
which identifies the operator of this service — remains visible in code
review.

`allowed-account-emails` holds the per-user email allow-list as a
comma-separated string, for example:

```
daniel@danielyoung.io,daniel.young@digitalhealthworks.com,<your-bw-email>
```

> **Why a tenant allow-list alone is insufficient.** The OAuth callback
> denies any sign-in whose `tid` is not in `ALLOWED_MICROSOFT_TENANT_IDS`,
> but a tenant such as DHW or BW contains every employee in that org, not
> just the people authorized to use this app. Without
> `ALLOWED_ACCOUNT_EMAILS`, the policy degrades to "anyone in an allow-
> listed tenant," which is far too broad for a tool that exposes a
> personal mailbox and downstream Graph data. Treating
> `allowed-account-emails` as a Key Vault-sourced secret keeps the
> per-user allow-list out of the repo and forces an explicit secret
> update (audited via Key Vault access logs) whenever a person is added
> or removed.

> **Warning:** `MICROSOFT_ENTRA_CLIENT_SECRET` must be sourced from the
> Key Vault secret holding the Entra client secret **value**
> (`dyc-comm-prod-value`), not the Entra secret **ID**
> (`dyc-comm-prod-sid`). Microsoft OAuth fails with `AADSTS7000215`
> ("Invalid client secret provided") when the secret ID is deployed in
> place of the secret value.

The OIDC principal used by `AZURE_CLIENT_ID` must hold a Key Vault
access policy or RBAC role that grants `get` on secrets in
`dyc-comm-prod-kv` (for example, `Key Vault Secrets User`). If a
required Key Vault secret is missing, disabled, or empty the workflow
fails before it touches the Container App.

Non-secret URLs (`MICROSOFT_ENTRA_REDIRECT_URI`, `WEB_APP_URL`,
`API_BASE_URL`, `ALLOWED_ORIGINS`), `APP_ENV`, the Key Vault name, and
the Decoding Options tenant id seed (`ALLOWED_MICROSOFT_TENANT_IDS_SEED`)
are committed in the workflow `env` block so the production runtime
contract is visible in code review. Values fetched from Key Vault —
including `dhw-tid`, `bw-tid`, and `allowed-account-emails` — are masked
with `::add-mask::` and stay in local shell variables inside a single
Azure CLI step; they are not written to `$GITHUB_ENV` or echoed in logs,
and the workflow validates that every fetched secret is non-empty before
calling `az containerapp update`.

Because the workflow currently materializes secret values into plain
Container App env vars (rather than wiring Container App Key Vault
secret refs), it sets `KEY_VAULT_REFS_ENABLED=false` to accurately
reflect that posture. Migrating to first-class Container App Key Vault
refs is tracked as follow-up work.

Trigger this workflow only when a runtime change is intentional. Trigger
it from the GitHub Actions UI on `main` (or another reviewed branch) so a
misconfigured branch cannot push values to production. The local
`apply-api-settings.sh` path remains supported for ad-hoc operator use.

## What is intentionally not automated

Neither the build/deploy workflows nor `configure-api-runtime.yml` runs
on push. Runtime updates are gated behind explicit `workflow_dispatch`
invocation so a regular `main` merge cannot blank or overwrite production
settings, and no workflow rotates secrets — Entra client secrets and the
database connection string are managed in Azure Key Vault
(`dyc-comm-prod-kv`) and only read from there at apply time.

## Verification

After applying settings, confirm the API is healthy and configured:

1. `curl https://api.comm.danielyoung.io/health`
2. `curl https://api.comm.danielyoung.io/config-check`
3. Web shell loads at `https://comm.danielyoung.io`.

`/config-check` reports which expected env vars are present and whether
`KEY_VAULT_REFS_ENABLED` is on; use it as the source of truth before
declaring an apply successful.

The response includes a `variables` object keyed by env-var name, each with
`present` (bool) and `is_secret` (bool) fields, plus a derived
`all_required_present` flag. Secret values are never returned — only
presence. Top-level `has_*` keys are kept for backward compatibility.

## Security and admin tasks

- **Rotate the previously committed MCP bearer token.** Treat it as
  compromised, revoke it, and issue a new one out-of-band. Do not paste
  the token value into commits, PRs, issues, or chat.
- **Keep secrets out of git.** `.env`, populated `infra/azure/api-runtime-settings.env`,
  and `.mcp.json` are gitignored — only their `.example` counterparts are
  tracked. Never commit a filled-in env file or a literal secret.
- **Prefer Key Vault refs for new secrets.** When adding a new secret env
  var, register it in Key Vault and wire a Container App ref instead of
  setting a plain value, unless you are explicitly in the interim state
  described above.
