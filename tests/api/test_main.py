from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from apps.api.app import main
from apps.api.app.main import app

client = TestClient(app)


RUNTIME_VARIABLES = (
    "DATABASE_URL",
    "MICROSOFT_ENTRA_CLIENT_ID",
    "MICROSOFT_ENTRA_TENANT_ID",
    "MICROSOFT_ENTRA_CLIENT_SECRET",
    "MICROSOFT_ENTRA_REDIRECT_URI",
    "KEY_VAULT_REFS_ENABLED",
)

SECRET_VARIABLES = {"DATABASE_URL", "MICROSOFT_ENTRA_CLIENT_SECRET"}


def _local_settings() -> main.Settings:
    return main.Settings(
        app_env="local",
        web_app_url="http://localhost:3000",
        api_base_url="http://localhost:8000",
        allowed_origins=["http://localhost:3000"],
        key_vault_refs_enabled=False,
        legacy_rule_folder_names=("Wolt", "Amazon", "Komote", "Cycle Touring"),
    )


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_config_check_defaults_when_unset(monkeypatch):
    for var in ("APP_ENV", *RUNTIME_VARIABLES):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(main, "settings", _local_settings())

    response = client.get("/config-check")
    assert response.status_code == 200
    payload = response.json()
    assert payload["environment"] == "local"
    assert payload["has_database_url"] is False
    assert payload["has_entra_client_id"] is False
    assert payload["has_entra_tenant_id"] is False
    assert payload["has_entra_client_secret"] is False
    assert payload["has_entra_redirect_uri"] is False
    assert payload["key_vault_refs_enabled"] is False
    assert payload["all_required_present"] is False

    variables = payload["variables"]
    assert set(variables.keys()) == set(RUNTIME_VARIABLES)
    for name in RUNTIME_VARIABLES:
        assert variables[name]["present"] is False
        assert variables[name]["is_secret"] is (name in SECRET_VARIABLES)


def test_config_check_reflects_set_env(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    secret_value = "super-secret-do-not-leak"
    redirect_value = "http://localhost:8000/auth/microsoft/callback"
    db_value = "postgresql://example"

    monkeypatch.setenv("DATABASE_URL", db_value)
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_ID", "client-id")
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", "tenant-id")
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_SECRET", secret_value)
    monkeypatch.setenv("MICROSOFT_ENTRA_REDIRECT_URI", redirect_value)
    monkeypatch.setenv("KEY_VAULT_REFS_ENABLED", "true")

    response = client.get("/config-check")
    assert response.status_code == 200
    payload = response.json()
    assert payload["environment"] == "local"
    assert payload["web_app_url"] == "http://localhost:3000"
    assert payload["api_base_url"] == "http://localhost:8000"
    assert payload["has_database_url"] is True
    assert payload["has_entra_client_id"] is True
    assert payload["has_entra_tenant_id"] is True
    assert payload["has_entra_client_secret"] is True
    assert payload["has_entra_redirect_uri"] is True
    assert payload["all_required_present"] is True

    variables = payload["variables"]
    for name in RUNTIME_VARIABLES:
        assert variables[name]["present"] is True
        assert variables[name]["is_secret"] is (name in SECRET_VARIABLES)

    body = response.text
    for sensitive in (secret_value, db_value, redirect_value, "client-id", "tenant-id"):
        assert sensitive not in body, f"value leaked into /config-check response: {sensitive!r}"


def test_key_vault_refs_enabled_parses_truthy(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("KEY_VAULT_REFS_ENABLED", value)
        monkeypatch.setattr(
            main,
            "settings",
            main.Settings(
                app_env="local",
                web_app_url="http://localhost:3000",
                api_base_url="http://localhost:8000",
                allowed_origins=["http://localhost:3000"],
                key_vault_refs_enabled=True,
                legacy_rule_folder_names=("Wolt",),
            ),
        )
        response = client.get("/config-check")
        assert response.json()["key_vault_refs_enabled"] is True, value

    for value in ("0", "false", "no", "", "off"):
        monkeypatch.setenv("KEY_VAULT_REFS_ENABLED", value)
        monkeypatch.setattr(main, "settings", _local_settings())
        response = client.get("/config-check")
        assert response.json()["key_vault_refs_enabled"] is False, value


def test_auth_session_returns_linked_account(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)

    response = test_client.get(
        "/auth/session",
        cookies={
            main.EMAIL_COOKIE: "daniel@danielyoung.io",
            main.NAME_COOKIE: "Daniel Young",
        },
    )

    assert response.status_code == 200
    assert response.json()["linked_account"] == {
        "email": "daniel@danielyoung.io",
        "display_name": "Daniel Young",
        "has_refresh_token": False,
    }
    assert response.json()["mailbox_access_ready"] is False


def test_auth_session_prefers_persisted_linked_account(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")

    def fake_load_linked_account(email: str):
        assert email == "daniel@danielyoung.io"
        return {
            "email": email,
            "display_name": "Daniel A. Young",
            "provider_account_id": "account-123",
            "has_refresh_token": True,
        }

    monkeypatch.setattr(main, "_load_linked_account", fake_load_linked_account)
    test_client = TestClient(main.app)

    response = test_client.get(
        "/auth/session",
        cookies={
            main.EMAIL_COOKIE: "daniel@danielyoung.io",
            main.NAME_COOKIE: "Daniel Young",
        },
    )

    assert response.status_code == 200
    assert response.json()["linked_account"] == {
        "email": "daniel@danielyoung.io",
        "display_name": "Daniel A. Young",
        "provider_account_id": "account-123",
        "has_refresh_token": True,
    }
    assert response.json()["mailbox_access_ready"] is True


def test_microsoft_start_sets_state_and_pkce(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", "tenant-id")
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_ID", "client-id")
    monkeypatch.setenv(
        "MICROSOFT_ENTRA_REDIRECT_URI",
        "http://localhost:8000/auth/microsoft/callback",
    )
    test_client = TestClient(main.app)

    response = test_client.get("/auth/microsoft/start", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "login.microsoftonline.com"
    assert query["client_id"] == ["client-id"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert main.AUTH_COOKIE in response.cookies
    assert main.PKCE_COOKIE in response.cookies


def test_microsoft_callback_redirects_to_web_on_success(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    persisted_profiles: list[dict[str, str]] = []

    async def fake_exchange(code: str, verifier: str):
        assert code == "sample-code"
        assert verifier == "stored-verifier"
        return {"access_token": "token"}

    async def fake_profile(access_token: str):
        assert access_token == "token"
        return {
            "mail": "daniel@danielyoung.io",
            "displayName": "Daniel Young",
        }

    monkeypatch.setattr(main, "_exchange_code", fake_exchange)
    monkeypatch.setattr(main, "_graph_profile", fake_profile)
    monkeypatch.setattr(
        main,
        "_persist_microsoft_account",
        lambda profile, token_response: persisted_profiles.append(
            {"profile": profile, "token_response": token_response}
        )
        or {
            "email": "daniel@danielyoung.io",
            "display_name": "Daniel Young",
            "provider_account_id": "account-123",
            "has_refresh_token": True,
        },
    )

    test_client = TestClient(main.app)
    response = test_client.get(
        "/auth/microsoft/callback?code=sample-code&state=stored-state",
        cookies={
            main.AUTH_COOKIE: "stored-state",
            main.PKCE_COOKIE: "stored-verifier",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == (
        "http://localhost:3000/?auth=success&account=daniel%40danielyoung.io"
    )
    assert persisted_profiles == [
        {
            "profile": {
                "mail": "daniel@danielyoung.io",
                "displayName": "Daniel Young",
            },
            "token_response": {
                "access_token": "token",
            },
        }
    ]
    assert main.EMAIL_COOKIE in response.cookies
    assert main.NAME_COOKIE in response.cookies


def test_microsoft_callback_redirects_with_error(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)

    response = test_client.get(
        "/auth/microsoft/callback?error=access_denied&error_description=No+consent",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == (
        "http://localhost:3000/?auth=error&reason=No+consent"
    )


def test_microsoft_callback_rejects_state_mismatch(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)

    response = test_client.get(
        "/auth/microsoft/callback?code=sample-code&state=unexpected-state",
        cookies={
            main.AUTH_COOKIE: "stored-state",
            main.PKCE_COOKIE: "stored-verifier",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "OAuth state mismatch."


def test_microsoft_callback_rejects_missing_verifier(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)

    response = test_client.get(
        "/auth/microsoft/callback?code=sample-code&state=stored-state",
        cookies={main.AUTH_COOKIE: "stored-state"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Missing PKCE verifier."


def test_auth_logout_clears_cookies():
    test_client = TestClient(main.app)
    response = test_client.post(
        "/auth/logout",
        cookies={
            main.EMAIL_COOKIE: "daniel@danielyoung.io",
            main.NAME_COOKIE: "Daniel Young",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "signed_out"}


def test_classify_folder_marks_legacy_rule_folder(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    folder = main._classify_folder({"displayName": "Wolt"})

    assert folder == {
        "ownership": "legacy_rule",
        "routing_state": "protected",
        "folder_role": "legacy_rule",
        "is_dyc_target": False,
        "canonical_name": "Wolt",
    }


def test_mail_folders_returns_graph_folders(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())

    async def fake_graph_access_token_for_email(email: str):
        assert email == "daniel@danielyoung.io"
        return "graph-token", {
            "account_id": "account-123",
            "email": email,
            "display_name": "Daniel Young",
        }

    async def fake_list_mail_folders(access_token: str, include_hidden: bool = False):
        assert access_token == "graph-token"
        assert include_hidden is True
        return [
            {"id": "inbox-id", "displayName": "Inbox"},
            {"id": "news-id", "displayName": "News"},
        ]

    monkeypatch.setattr(main, "_graph_access_token_for_email", fake_graph_access_token_for_email)
    monkeypatch.setattr(main, "_list_mail_folders", fake_list_mail_folders)
    test_client = TestClient(main.app)

    response = test_client.get(
        "/mail/folders?include_hidden=true",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "account": {
            "email": "daniel@danielyoung.io",
            "display_name": "Daniel Young",
        },
        "folders": [
            {
                "id": "inbox-id",
                "displayName": "Inbox",
                "ownership": "system",
                "routing_state": "observed",
                "folder_role": "system",
                "is_dyc_target": False,
                "canonical_name": "Inbox",
            },
            {
                "id": "news-id",
                "displayName": "News",
                "ownership": "dyc_managed",
                "routing_state": "active",
                "folder_role": "20 - News",
                "is_dyc_target": True,
                "canonical_name": "20 - News",
            },
        ],
    }


def test_mail_folders_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)
    response = test_client.get("/mail/folders")
    assert response.status_code == 401
    assert response.json()["detail"] == "No linked account session found."
