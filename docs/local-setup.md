# Local setup

This guide gets the FastAPI service running locally and explains how secrets
are handled.

## Prerequisites

- Python 3.12
- Docker (optional, for parity with the production container)
- PostgreSQL 14+ (only needed once schema-backed features land)

## API service

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r apps/api/requirements-dev.txt

cp apps/api/.env.example apps/api/.env
# edit apps/api/.env with real values — never commit it

# load .env into your shell however you prefer (direnv, dotenv-cli, etc.)
uvicorn apps.api.app.main:app --reload --port 8000
```

Smoke-test:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/config-check
```

## Tests, lint, format

```bash
pytest
ruff check apps tests
ruff format apps tests
```

CI runs the same commands — see `.github/workflows/ci.yml`.

## Database schema

The executable schema lives in `migrations/0001_initial.sql`; `schema.md` is
the human-readable design doc. To apply locally:

```bash
createdb dyc_comm
psql "$DATABASE_URL" -f migrations/0001_initial.sql
```

## Required environment variables

See `apps/api/.env.example` for the full list. Summary:

| Variable | Purpose |
| --- | --- |
| `APP_ENV` | `local`, `staging`, or `production`. |
| `DATABASE_URL` | PostgreSQL connection string. |
| `MICROSOFT_ENTRA_CLIENT_ID` | Azure AD app registration client id. |
| `MICROSOFT_ENTRA_TENANT_ID` | Azure AD tenant id. |
| `MICROSOFT_ENTRA_CLIENT_SECRET` | Azure AD client secret (Key Vault in prod). |
| `MICROSOFT_ENTRA_REDIRECT_URI` | OAuth callback URL. |
| `KEY_VAULT_REFS_ENABLED` | `true` when secrets resolve via Key Vault refs. |

## Secret handling rules

- **Never commit `.env` files, populated `secrets.*`, or `.mcp.json`.** They
  are gitignored. Copy from the corresponding `.example` file and fill in
  real values locally.
- Production reads secrets from Azure Key Vault via Container Apps refs.
  Set `KEY_VAULT_REFS_ENABLED=true` so `/config-check` reflects that. See
  [`docs/azure-runtime-config.md`](azure-runtime-config.md) for the
  Container App settings inventory and the apply helper.
- The MCP agent config (`.mcp.json`) takes a bearer token via the
  `AUTH_TOKEN` environment variable. The committed template
  (`.mcp.json.example`) intentionally contains no literal token.

## Immediate next steps (admin)

1. **Rotate the previously committed MCP bearer token.** The token must be
   considered compromised; revoke it and issue a new one. Track this as a
   separate manual task — it is outside the scope of this PR.
2. **Scrub the token from git history.** Even after this PR removes the
   token from the working tree, anything previously pushed remains in the
   history of the affected branch(es). Use `git filter-repo` (or the
   GitHub support flow for secret purging) and force-push only with
   coordination across collaborators. This is destructive and intentionally
   not done automatically here.
3. **Decide on `fix-acr-login-server`.** That branch is significantly ahead
   of `main` (Microsoft mailbox auth, expanded API, real test suite, ACR
   login fixes, runtime config docs). Triage recommendation: split it into
   reviewable PRs targeting `main` rather than fast-forwarding the branch
   wholesale, because it bundles unrelated changes (auth, frontend, infra,
   docs) that benefit from independent review.
