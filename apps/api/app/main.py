import os

from fastapi import FastAPI


def _bool_env(name: str) -> bool:
    value = os.getenv(name, "")
    return value.lower() in {"1", "true", "yes", "on"}


# Runtime env-var contract reported by /config-check. Mirrors the variables
# documented in docs/azure-runtime-config.md and applied by
# infra/azure/apply-api-settings.sh. Values are never returned — only presence
# and whether a variable carries secret material.
_RUNTIME_VARIABLES: tuple[tuple[str, bool], ...] = (
    ("DATABASE_URL", True),
    ("MICROSOFT_ENTRA_CLIENT_ID", False),
    ("MICROSOFT_ENTRA_TENANT_ID", False),
    ("MICROSOFT_ENTRA_CLIENT_SECRET", True),
    ("MICROSOFT_ENTRA_REDIRECT_URI", False),
    ("KEY_VAULT_REFS_ENABLED", False),
)


app = FastAPI(
    title="DYC Comm API",
    version="0.1.0",
    description="Minimal API scaffold for Azure Container Apps deployment.",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/config-check")
def config_check() -> dict[str, object]:
    variables: dict[str, dict[str, bool]] = {
        name: {"present": bool(os.getenv(name)), "is_secret": is_secret}
        for name, is_secret in _RUNTIME_VARIABLES
    }

    return {
        "environment": os.getenv("APP_ENV", "production"),
        "variables": variables,
        "all_required_present": all(v["present"] for v in variables.values()),
        "has_database_url": variables["DATABASE_URL"]["present"],
        "has_entra_client_id": variables["MICROSOFT_ENTRA_CLIENT_ID"]["present"],
        "has_entra_tenant_id": variables["MICROSOFT_ENTRA_TENANT_ID"]["present"],
        "has_entra_client_secret": variables["MICROSOFT_ENTRA_CLIENT_SECRET"]["present"],
        "has_entra_redirect_uri": variables["MICROSOFT_ENTRA_REDIRECT_URI"]["present"],
        "key_vault_refs_enabled": _bool_env("KEY_VAULT_REFS_ENABLED"),
    }
