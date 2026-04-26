from fastapi.testclient import TestClient

from apps.api.app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_config_check_defaults_when_unset(monkeypatch):
    for var in (
        "APP_ENV",
        "DATABASE_URL",
        "MICROSOFT_ENTRA_CLIENT_ID",
        "MICROSOFT_ENTRA_TENANT_ID",
        "MICROSOFT_ENTRA_CLIENT_SECRET",
        "MICROSOFT_ENTRA_REDIRECT_URI",
        "KEY_VAULT_REFS_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)

    response = client.get("/config-check")
    assert response.status_code == 200
    payload = response.json()
    assert payload["environment"] == "production"
    assert payload["has_database_url"] is False
    assert payload["has_entra_client_id"] is False
    assert payload["has_entra_tenant_id"] is False
    assert payload["has_entra_client_secret"] is False
    assert payload["has_entra_redirect_uri"] is False
    assert payload["key_vault_refs_enabled"] is False


def test_config_check_reflects_set_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_ID", "client-id")
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", "tenant-id")
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_SECRET", "secret")
    monkeypatch.setenv(
        "MICROSOFT_ENTRA_REDIRECT_URI",
        "http://localhost:8000/auth/microsoft/callback",
    )
    monkeypatch.setenv("KEY_VAULT_REFS_ENABLED", "true")

    response = client.get("/config-check")
    assert response.status_code == 200
    payload = response.json()
    assert payload["environment"] == "local"
    assert payload["has_database_url"] is True
    assert payload["has_entra_client_id"] is True
    assert payload["has_entra_tenant_id"] is True
    assert payload["has_entra_client_secret"] is True
    assert payload["has_entra_redirect_uri"] is True
    assert payload["key_vault_refs_enabled"] is True


def test_key_vault_refs_enabled_parses_truthy(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("KEY_VAULT_REFS_ENABLED", value)
        response = client.get("/config-check")
        assert response.json()["key_vault_refs_enabled"] is True, value

    for value in ("0", "false", "no", "", "off"):
        monkeypatch.setenv("KEY_VAULT_REFS_ENABLED", value)
        response = client.get("/config-check")
        assert response.json()["key_vault_refs_enabled"] is False, value
