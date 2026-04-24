import os

from fastapi import FastAPI


def _bool_env(name: str) -> bool:
    value = os.getenv(name, "")
    return value.lower() in {"1", "true", "yes", "on"}


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
    return {
        "environment": os.getenv("APP_ENV", "production"),
        "has_database_url": bool(os.getenv("DATABASE_URL")),
        "has_entra_client_id": bool(os.getenv("MICROSOFT_ENTRA_CLIENT_ID")),
        "has_entra_tenant_id": bool(os.getenv("MICROSOFT_ENTRA_TENANT_ID")),
        "has_entra_client_secret": bool(os.getenv("MICROSOFT_ENTRA_CLIENT_SECRET")),
        "has_entra_redirect_uri": bool(os.getenv("MICROSOFT_ENTRA_REDIRECT_URI")),
        "key_vault_refs_enabled": _bool_env("KEY_VAULT_REFS_ENABLED"),
    }

