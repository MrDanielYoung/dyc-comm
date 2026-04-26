# Azure Runtime Configuration

## Purpose

This document defines the runtime configuration expected by the current DYC Comm API and web deployments in Azure Container Apps.

The goal is to make production updates predictable and reviewable before environment values are changed in the portal or automated through GitHub Actions.

## Container Apps

Current target apps:

* `dyc-comm-prod-web`
* `dyc-comm-prod-api`

Shared environment:

* `dyc-comm-prod-cenv`

## Web App

The current web shell is static HTML served from Nginx.

Expected runtime behavior:

* public ingress enabled
* target port `80`
* no required secrets

Current UI behavior:

* defaults to `https://api.comm.danielyoung.io`
* if hosted on `comm.<domain>`, it infers `api.<same-domain>`
* supports an explicit `?api=` override for local or temporary testing

## API App

The API now includes:

* `/health`
* `/config-check`
* `/auth/session`
* `/auth/microsoft/start`
* `/auth/microsoft/callback`
* `/auth/logout`

Expected app settings for `dyc-comm-prod-api`:

* `APP_ENV=production`
* `DATABASE_URL=<postgres connection string>`
* `MICROSOFT_ENTRA_CLIENT_ID=<Entra app client id>`
* `MICROSOFT_ENTRA_TENANT_ID=<Entra tenant id>`
* `MICROSOFT_ENTRA_CLIENT_SECRET=<Entra app client secret>`
* `MICROSOFT_ENTRA_REDIRECT_URI=https://api.comm.danielyoung.io/auth/microsoft/callback`
* `WEB_APP_URL=https://comm.danielyoung.io`
* `API_BASE_URL=https://api.comm.danielyoung.io`
* `ALLOWED_ORIGINS=https://comm.danielyoung.io`
* `KEY_VAULT_REFS_ENABLED=true`

Ingress expectations:

* public ingress enabled
* target port `80`

## Secret Handling

Preferred steady-state:

* non-secret values stored as plain Container App environment variables
* secrets sourced from Azure Key Vault references where practical

Near-term acceptable fallback:

* set the required values directly in the Container App while the full Key Vault reference pattern is being wired

## Current Safe Deployment Position

The GitHub deployment workflows are working for code and image rollout.

What is still intentionally not automated in the workflow:

* setting production runtime values for the API
* rotating Microsoft client secrets
* writing database connection strings into the app

This is deliberate for now to avoid pushing blank or incorrect production settings during overnight development.

## Morning Verification Checklist

When runtime values are present in `dyc-comm-prod-api`, verify:

1. `https://api.comm.danielyoung.io/health`
2. `https://api.comm.danielyoung.io/config-check`
3. web load at `https://comm.danielyoung.io`
4. Microsoft auth start redirects correctly
5. Microsoft callback returns to the web app and shows a linked session

## Recommended Next Infrastructure Step

After the current auth slice is validated:

* move API secrets to Key Vault-backed references in `dyc-comm-prod-api`
* document the exact Container App revision config
* add a deliberate deploy update for runtime env values instead of relying on portal-only drift

## Repo-Native Runtime Update Path

This repository now includes:

* `infra/azure/api-runtime-settings.env.example`
* `infra/azure/apply-api-settings.sh`
* `.github/workflows/configure-api-runtime.yml`

Use these to keep API runtime configuration explicit and reviewable instead of editing production settings ad hoc.

The GitHub workflow expects additional repository or environment secrets:

* `DATABASE_URL`
* `MICROSOFT_ENTRA_CLIENT_ID`
* `MICROSOFT_ENTRA_TENANT_ID`
* `MICROSOFT_ENTRA_CLIENT_SECRET`
