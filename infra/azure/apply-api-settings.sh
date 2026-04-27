#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <env-file>"
  exit 1
fi

ENV_FILE="$1"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Environment file not found: $ENV_FILE"
  exit 1
fi

RESOURCE_GROUP="${RESOURCE_GROUP:-dyc-comm-prod-rg}"
CONTAINER_APP_NAME="${CONTAINER_APP_NAME:-dyc-comm-prod-api}"

mapfile -t SETTINGS < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE")

if [[ ${#SETTINGS[@]} -eq 0 ]]; then
  echo "No settings found in $ENV_FILE"
  exit 1
fi

echo "Updating $CONTAINER_APP_NAME in $RESOURCE_GROUP"
az containerapp update \
  --name "$CONTAINER_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --set-env-vars "${SETTINGS[@]}"
