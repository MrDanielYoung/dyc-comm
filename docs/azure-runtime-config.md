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
| `KEY_VAULT_REFS_ENABLED` | `true` when secrets resolve via Key Vault refs. |

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

## What is intentionally not automated

The GitHub workflows in `.github/workflows/` build and deploy code and
images, but they do **not** push API runtime values to the Container App.
Until a vetted runtime-config workflow lands, runtime settings are applied
manually with `infra/azure/apply-api-settings.sh` so a misconfigured run
cannot blank or overwrite production settings.

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
