# Azure Infrastructure Helpers

Standalone helpers for managing the production Azure Container Apps deployment of the DYC Comm API. These scripts wrap `az` CLI calls so runtime configuration changes are explicit, reviewable, and reproducible instead of being applied ad hoc through the Azure Portal.

## Files

* `api-runtime-settings.env.example` — template listing the environment variables the API container app expects at runtime. Copy to a private, gitignored file (e.g. `api-runtime-settings.env`) and fill in real values before applying.
* `apply-api-settings.sh` — Bash helper that reads a key=value env file and pushes the entries to the target Container App with `az containerapp update --set-env-vars`.

## Prerequisites

* Azure CLI (`az`) installed and authenticated against the target subscription (`az login`).
* Permission to update the target Container App.
* Bash 4+ (the script uses `mapfile`).

## Usage

```bash
# 1. Copy the template and fill in real values locally. Do NOT commit the result.
cp infra/azure/api-runtime-settings.env.example infra/azure/api-runtime-settings.env

# 2. Apply to the default Container App (dyc-comm-prod-api in dyc-comm-prod-rg).
./infra/azure/apply-api-settings.sh infra/azure/api-runtime-settings.env

# 3. Or override the target app / resource group via env vars.
RESOURCE_GROUP=my-rg CONTAINER_APP_NAME=my-api \
  ./infra/azure/apply-api-settings.sh infra/azure/api-runtime-settings.env
```

The script reads any line in the env file that starts with `KEY=` and forwards them to `az containerapp update --set-env-vars`. Lines that do not match that shape (comments, blank lines) are ignored.

## Defaults

| Variable             | Default                |
| -------------------- | ---------------------- |
| `RESOURCE_GROUP`     | `dyc-comm-prod-rg`     |
| `CONTAINER_APP_NAME` | `dyc-comm-prod-api`    |

## Secret handling

The example file lists settings as plain key/value pairs for clarity. In production, prefer Azure Key Vault references for secret values (client secrets, database connection strings) and keep only non-secret configuration as plain environment variables.

Do not commit a filled-in env file — keep it local or store values in a secret manager.
