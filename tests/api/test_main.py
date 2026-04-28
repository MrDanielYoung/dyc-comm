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


def test_microsoft_start_omits_login_hint_by_default(monkeypatch):
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
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert "login_hint" not in query


def test_microsoft_start_propagates_login_hint(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", "tenant-id")
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_ID", "client-id")
    monkeypatch.setenv(
        "MICROSOFT_ENTRA_REDIRECT_URI",
        "http://localhost:8000/auth/microsoft/callback",
    )
    test_client = TestClient(main.app)

    response = test_client.get(
        "/auth/microsoft/start?login_hint=daniel.young@digitalhealthworks.com",
        follow_redirects=False,
    )

    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["login_hint"] == ["daniel.young@digitalhealthworks.com"]
    assert query["prompt"] == ["select_account"]


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
    assert response.headers["location"] == ("http://localhost:3000/?auth=error&reason=No+consent")


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


def test_bootstrap_mail_folders_ensures_defaults(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    persisted_inventory: list[tuple[str, list[dict[str, object]]]] = []

    async def fake_graph_access_token_for_email(email: str):
        assert email == "daniel@danielyoung.io"
        return "graph-token", {
            "account_id": "account-123",
            "email": email,
            "display_name": "Daniel Young",
        }

    async def fake_ensure_default_mail_folders(access_token: str):
        assert access_token == "graph-token"
        return [
            {"id": "review-id", "displayName": "10 - Review"},
            {"id": "news-id", "displayName": "20 - News"},
        ]

    monkeypatch.setattr(main, "_graph_access_token_for_email", fake_graph_access_token_for_email)
    monkeypatch.setattr(main, "_ensure_default_mail_folders", fake_ensure_default_mail_folders)
    monkeypatch.setattr(
        main,
        "_persist_folder_inventory",
        lambda account_id, folders: persisted_inventory.append((account_id, folders)),
    )
    test_client = TestClient(main.app)

    response = test_client.post(
        "/mail/folders/bootstrap",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["account"] == {
        "email": "daniel@danielyoung.io",
        "display_name": "Daniel Young",
    }
    assert body["ensured_folders"] == [
        {
            "id": "review-id",
            "displayName": "10 - Review",
            "ownership": "dyc_managed",
            "routing_state": "active",
            "folder_role": "10 - Review",
            "is_dyc_target": True,
            "canonical_name": "10 - Review",
        },
        {
            "id": "news-id",
            "displayName": "20 - News",
            "ownership": "dyc_managed",
            "routing_state": "active",
            "folder_role": "20 - News",
            "is_dyc_target": True,
            "canonical_name": "20 - News",
        },
    ]
    assert persisted_inventory
    assert persisted_inventory[0][0] == "account-123"


def test_bootstrap_mail_folders_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)
    response = test_client.post("/mail/folders/bootstrap")
    assert response.status_code == 401
    assert response.json()["detail"] == "No linked account session found."


def test_ensure_default_mail_folders_creates_missing_only(monkeypatch):
    existing_folders = [
        {"id": "review-id", "displayName": "Review"},
        {"id": "news-id", "displayName": "News"},
        {"id": "linkedin-id", "displayName": "LinkedIn"},
    ]
    created_payloads: list[dict[str, str]] = []

    async def fake_list_mail_folders(access_token: str, include_hidden: bool = False):
        assert access_token == "graph-token"
        assert include_hidden is False
        return existing_folders

    async def fake_graph_post(access_token: str, path: str, payload: dict[str, str]):
        assert access_token == "graph-token"
        assert path == "/me/mailFolders"
        created_payloads.append(payload)
        return {"id": "created-id", "displayName": payload["displayName"]}

    monkeypatch.setattr(main, "_list_mail_folders", fake_list_mail_folders)
    monkeypatch.setattr(main, "_graph_post", fake_graph_post)

    import asyncio

    result = asyncio.run(main._ensure_default_mail_folders("graph-token"))

    assert result == [
        {"id": "review-id", "displayName": "Review"},
        {"id": "news-id", "displayName": "News"},
        {"id": "linkedin-id", "displayName": "LinkedIn"},
        {"id": "created-id", "displayName": "40 - Notifications"},
        {"id": "created-id", "displayName": "50 - Marketing"},
        {"id": "created-id", "displayName": "60 - Notes"},
        {"id": "created-id", "displayName": "70 - Contracts"},
        {"id": "created-id", "displayName": "80 - Travel"},
        {"id": "created-id", "displayName": "90 - IT Reports"},
    ]
    assert created_payloads == [
        {"displayName": "40 - Notifications"},
        {"displayName": "50 - Marketing"},
        {"displayName": "60 - Notes"},
        {"displayName": "70 - Contracts"},
        {"displayName": "80 - Travel"},
        {"displayName": "90 - IT Reports"},
    ]


def test_sync_mail_folder_inventory_persists_annotated_folders(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    persisted_inventory: list[tuple[str, list[dict[str, object]]]] = []

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
            {"id": "wolt-id", "displayName": "Wolt"},
            {"id": "review-id", "displayName": "10 - Review"},
        ]

    monkeypatch.setattr(main, "_graph_access_token_for_email", fake_graph_access_token_for_email)
    monkeypatch.setattr(main, "_list_mail_folders", fake_list_mail_folders)
    monkeypatch.setattr(
        main,
        "_persist_folder_inventory",
        lambda account_id, folders: persisted_inventory.append((account_id, folders)),
    )
    test_client = TestClient(main.app)

    response = test_client.post(
        "/mail/folders/inventory/sync",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    assert response.json()["folders"] == [
        {
            "id": "wolt-id",
            "displayName": "Wolt",
            "ownership": "legacy_rule",
            "routing_state": "protected",
            "folder_role": "legacy_rule",
            "is_dyc_target": False,
            "canonical_name": "Wolt",
        },
        {
            "id": "review-id",
            "displayName": "10 - Review",
            "ownership": "dyc_managed",
            "routing_state": "active",
            "folder_role": "10 - Review",
            "is_dyc_target": True,
            "canonical_name": "10 - Review",
        },
    ]
    assert persisted_inventory
    assert persisted_inventory[0][0] == "account-123"


def test_mail_folder_inventory_returns_persisted_rows(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())

    monkeypatch.setattr(
        main,
        "_load_account_credentials",
        lambda email: {
            "account_id": "account-123",
            "email": email,
            "display_name": "Daniel Young",
        },
    )
    monkeypatch.setattr(
        main,
        "_load_folder_inventory",
        lambda account_id: [
            {
                "id": "wolt-id",
                "displayName": "Wolt",
                "ownership": "legacy_rule",
                "routing_state": "protected",
                "folder_role": "legacy_rule",
                "is_dyc_target": False,
                "canonical_name": "Wolt",
            }
        ],
    )
    test_client = TestClient(main.app)

    response = test_client.get(
        "/mail/folders/inventory",
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
                "id": "wolt-id",
                "displayName": "Wolt",
                "ownership": "legacy_rule",
                "routing_state": "protected",
                "folder_role": "legacy_rule",
                "is_dyc_target": False,
                "canonical_name": "Wolt",
            }
        ],
    }


def test_mail_folder_inventory_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)
    response = test_client.get("/mail/folders/inventory")
    assert response.status_code == 401


def test_sync_mail_folder_inventory_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)
    response = test_client.post("/mail/folders/inventory/sync")
    assert response.status_code == 401
    assert response.json()["detail"] == "No linked account session found."


def test_auth_session_without_cookie_returns_no_linked_account(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)
    response = test_client.get("/auth/session")
    assert response.status_code == 200
    payload = response.json()
    assert payload["linked_account"] is None
    assert payload["mailbox_access_ready"] is False


def test_protected_mailbox_endpoints_reject_unauthenticated_calls(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)

    protected = (
        ("GET", "/mail/folders"),
        ("GET", "/mail/folders/inventory"),
        ("POST", "/mail/folders/bootstrap"),
        ("POST", "/mail/folders/inventory/sync"),
        ("POST", "/mail/messages/sync"),
        ("GET", "/accounts"),
        ("GET", "/dashboard/summary"),
        ("GET", "/accounts/daniel@danielyoung.io/dashboard"),
        ("GET", "/activity"),
        ("GET", "/alerts"),
    )
    for method, path in protected:
        response = test_client.request(method, path)
        assert response.status_code == 401, f"{method} {path} should require a session"
        assert response.json()["detail"] == "No linked account session found."


def test_accounts_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)
    response = test_client.get("/accounts")
    assert response.status_code == 401
    assert response.json()["detail"] == "No linked account session found."


def test_accounts_returns_session_only_when_no_db(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [])
    test_client = TestClient(main.app)

    response = test_client.get(
        "/accounts",
        cookies={
            main.EMAIL_COOKIE: "daniel@danielyoung.io",
            main.NAME_COOKIE: "Daniel Young",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user"] == {"email": "daniel@danielyoung.io"}
    assert payload["accounts"] == [
        {
            "account_id": None,
            "provider": main.MICROSOFT_PROVIDER,
            "email": "daniel@danielyoung.io",
            "display_name": "Daniel Young",
            "status": "session_only",
            "mailbox_access_ready": False,
            "token_updated_at": None,
            "created_at": None,
            "updated_at": None,
        }
    ]


def test_accounts_returns_persisted_rows(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: [
            {
                "account_id": "account-123",
                "provider": main.MICROSOFT_PROVIDER,
                "email": email,
                "display_name": "Daniel Young",
                "status": "active",
                "has_refresh_token": True,
                "token_updated_at": "2026-04-27T12:00:00+00:00",
                "updated_at": "2026-04-28T01:00:00+00:00",
                "created_at": "2026-04-01T00:00:00+00:00",
            }
        ],
    )
    test_client = TestClient(main.app)

    response = test_client.get(
        "/accounts",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accounts"][0]["account_id"] == "account-123"
    assert payload["accounts"][0]["status"] == "active"
    assert payload["accounts"][0]["mailbox_access_ready"] is True


def test_dashboard_summary_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)
    response = test_client.get("/dashboard/summary")
    assert response.status_code == 401
    assert response.json()["detail"] == "No linked account session found."


def test_dashboard_summary_session_only_view(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [])
    test_client = TestClient(main.app)

    response = test_client.get(
        "/dashboard/summary",
        cookies={
            main.EMAIL_COOKIE: "daniel@danielyoung.io",
            main.NAME_COOKIE: "Daniel Young",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user"] == {"email": "daniel@danielyoung.io"}
    assert payload["window_days"] == 7
    assert payload["supported_window_days"] == [1, 7, 30]
    totals = payload["totals"]
    assert totals["connected_accounts"] == 1
    assert totals["mailbox_ready_accounts"] == 0
    assert totals["total_folders"] == 0
    assert totals["dyc_target_folders"] == 0
    # New roll-up tiles default to zero in session-only mode.
    assert totals["messages_in"] == 0
    assert totals["messages_persisted"] == 0
    assert totals["messages_moved"] == 0
    assert totals["errors"] == 0
    assert payload["accounts"][0]["account"]["status"] == "session_only"
    assert payload["accounts"][0]["folder_inventory"]["available"] is False
    assert payload["accounts"][0]["email_volume"]["available"] is False
    assert payload["accounts"][0]["action_activity"]["available"] is False
    pending = {entry["metric"] for entry in payload["pending_instrumentation"]}
    # email_volume is no longer in pending_instrumentation: the volume tile
    # surfaces an honest empty state with `available=false` until a sync runs,
    # rather than being permanently blocked behind a connector worker.
    assert pending == {"action_activity"}


def test_dashboard_summary_aggregates_persisted_account(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: [
            {
                "account_id": "account-123",
                "provider": main.MICROSOFT_PROVIDER,
                "email": email,
                "display_name": "Daniel Young",
                "status": "active",
                "has_refresh_token": True,
                "token_updated_at": "2026-04-27T12:00:00+00:00",
                "updated_at": "2026-04-28T01:00:00+00:00",
                "created_at": "2026-04-01T00:00:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(
        main,
        "_load_folder_inventory",
        lambda account_id: [
            {
                "id": "review-id",
                "displayName": "10 - Review",
                "ownership": "dyc_managed",
                "is_dyc_target": True,
            },
            {
                "id": "wolt-id",
                "displayName": "Wolt",
                "ownership": "legacy_rule",
                "is_dyc_target": False,
            },
            {
                "id": "inbox-id",
                "displayName": "Inbox",
                "ownership": "system",
                "is_dyc_target": False,
            },
        ],
    )
    test_client = TestClient(main.app)

    response = test_client.get(
        "/dashboard/summary",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"]["connected_accounts"] == 1
    assert payload["totals"]["mailbox_ready_accounts"] == 1
    assert payload["totals"]["total_folders"] == 3
    assert payload["totals"]["dyc_target_folders"] == 1
    # New roll-up tiles default to zero with no DATABASE_URL set.
    assert payload["totals"]["messages_in"] == 0
    assert payload["totals"]["messages_moved"] == 0

    account_entry = payload["accounts"][0]
    assert account_entry["account"]["mailbox_access_ready"] is True
    assert account_entry["folder_inventory"] == {
        "available": True,
        "total_folders": 3,
        "dyc_target_folders": 1,
        "by_ownership": {"dyc_managed": 1, "legacy_rule": 1, "system": 1},
        "expected_dyc_target_count": len(main.DEFAULT_MVP_FOLDER_SPECS),
        "is_bootstrapped": False,
    }
    assert account_entry["email_volume"]["available"] is False
    # The empty volume metrics now report 0 (and not None) for counters so the
    # dashboard can render an honest empty state without null-handling.
    assert account_entry["email_volume"]["messages_in"] == 0
    assert account_entry["email_volume"]["messages_persisted"] == 0
    assert account_entry["email_volume"]["window_days"] == 7
    assert account_entry["action_activity"]["available"] is False


def test_account_dashboard_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)
    response = test_client.get("/accounts/daniel@danielyoung.io/dashboard")
    assert response.status_code == 401


def test_account_dashboard_returns_404_for_unknown_email(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [])
    test_client = TestClient(main.app)

    response = test_client.get(
        "/accounts/missing@example.com/dashboard",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 404


def test_account_dashboard_returns_payload_for_match(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: [
            {
                "account_id": "account-123",
                "provider": main.MICROSOFT_PROVIDER,
                "email": email,
                "display_name": "Daniel Young",
                "status": "active",
                "has_refresh_token": True,
                "token_updated_at": None,
                "updated_at": None,
                "created_at": None,
            }
        ],
    )
    monkeypatch.setattr(main, "_load_folder_inventory", lambda account_id: [])
    test_client = TestClient(main.app)

    response = test_client.get(
        "/accounts/daniel@danielyoung.io/dashboard",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["account"]["account_id"] == "account-123"
    assert payload["folder_inventory"]["available"] is True
    assert payload["folder_inventory"]["total_folders"] == 0
    assert payload["email_volume"]["available"] is False
    assert payload["action_activity"]["available"] is False


def test_cli_build_parser_dispatches_subcommands():
    from apps.api.app import cli

    parser = cli.build_parser()

    folders_args = parser.parse_args(["folders", "--include-hidden"])
    assert folders_args.func is cli.cmd_folders
    assert folders_args.include_hidden is True

    bootstrap_args = parser.parse_args(["bootstrap"])
    assert bootstrap_args.func is cli.cmd_bootstrap

    inventory_args = parser.parse_args(["inventory"])
    assert inventory_args.func is cli.cmd_inventory

    sync_args = parser.parse_args(["inventory-sync"])
    assert sync_args.func is cli.cmd_inventory_sync
    assert sync_args.include_hidden is True

    status_args = parser.parse_args(["status"])
    assert status_args.func is cli.cmd_status

    session_args = parser.parse_args(["session"])
    assert session_args.func is cli.cmd_session

    auth_args = parser.parse_args(["auth-url"])
    assert auth_args.func is cli.cmd_auth_url


def test_activity_returns_empty_when_no_accounts(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [])
    test_client = TestClient(main.app)

    response = test_client.get(
        "/activity",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["folder_activity"]["available"] is False
    assert payload["folder_activity"]["events"] == []
    assert payload["message_movement"]["available"] is False
    assert payload["message_movement"]["events"] == []
    assert any(item["metric"] == "message_movement" for item in payload["pending_instrumentation"])


def test_activity_includes_folder_events(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: [
            {
                "account_id": "account-123",
                "provider": main.MICROSOFT_PROVIDER,
                "email": email,
                "display_name": "Daniel Young",
                "status": "active",
                "has_refresh_token": True,
                "token_updated_at": None,
                "updated_at": None,
                "created_at": None,
            }
        ],
    )

    def fake_load_folder_activity(account_id, limit):
        assert account_id == "account-123"
        return [
            {
                "event_type": "folder.bootstrap",
                "occurred_at": "2026-04-28T10:00:00+00:00",
                "folder": {
                    "provider_folder_id": "review-id",
                    "display_name": "10 - Review",
                    "canonical_name": "10 - Review",
                    "ownership": "dyc_managed",
                    "is_dyc_target": True,
                },
            },
            {
                "event_type": "folder.sync",
                "occurred_at": "2026-04-27T09:00:00+00:00",
                "folder": {
                    "provider_folder_id": "wolt-id",
                    "display_name": "Wolt",
                    "canonical_name": "Wolt",
                    "ownership": "legacy_rule",
                    "is_dyc_target": False,
                },
            },
        ]

    monkeypatch.setattr(main, "_load_folder_activity", fake_load_folder_activity)
    test_client = TestClient(main.app)

    response = test_client.get(
        "/activity",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["folder_activity"]["available"] is True
    events = payload["folder_activity"]["events"]
    assert len(events) == 2
    assert events[0]["event_type"] == "folder.bootstrap"
    assert events[0]["account"] == {
        "account_id": "account-123",
        "email": "daniel@danielyoung.io",
    }
    assert events[1]["folder"]["display_name"] == "Wolt"


def test_alerts_flags_no_connected_accounts_and_runtime(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    for var, _ in main._RUNTIME_VARIABLES:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [])
    test_client = TestClient(main.app)

    response = test_client.get(
        "/alerts",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    codes = {item["code"] for item in payload["alerts"]}
    assert "runtime_config_missing" in codes
    assert "mailbox_access_not_ready" in codes
    assert "move_worker_pending" in codes
    assert "database_unavailable" in codes
    assert payload["counts"]["error"] >= 1


def test_alerts_flags_folder_inventory_missing(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    for var in (
        "MICROSOFT_ENTRA_CLIENT_ID",
        "MICROSOFT_ENTRA_TENANT_ID",
        "MICROSOFT_ENTRA_CLIENT_SECRET",
        "MICROSOFT_ENTRA_REDIRECT_URI",
    ):
        monkeypatch.setenv(var, "x")

    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: [
            {
                "account_id": "account-123",
                "provider": main.MICROSOFT_PROVIDER,
                "email": email,
                "display_name": "Daniel Young",
                "status": "active",
                "has_refresh_token": True,
                "token_updated_at": None,
                "updated_at": None,
                "created_at": None,
            }
        ],
    )
    monkeypatch.setattr(main, "_load_folder_inventory", lambda account_id: [])
    test_client = TestClient(main.app)

    response = test_client.get(
        "/alerts",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    codes = {item["code"] for item in response.json()["alerts"]}
    assert "folder_inventory_missing" in codes
    assert "no_connected_accounts" not in codes
    assert "runtime_config_missing" not in codes


def test_alerts_clean_when_state_is_healthy(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    for var in (
        "MICROSOFT_ENTRA_CLIENT_ID",
        "MICROSOFT_ENTRA_TENANT_ID",
        "MICROSOFT_ENTRA_CLIENT_SECRET",
        "MICROSOFT_ENTRA_REDIRECT_URI",
    ):
        monkeypatch.setenv(var, "x")

    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: [
            {
                "account_id": "account-123",
                "provider": main.MICROSOFT_PROVIDER,
                "email": email,
                "display_name": "Daniel Young",
                "status": "active",
                "has_refresh_token": True,
                "token_updated_at": None,
                "updated_at": None,
                "created_at": None,
            }
        ],
    )

    def fake_summarize(account_id):
        return {
            "available": True,
            "total_folders": len(main.DEFAULT_MVP_FOLDER_SPECS) + 5,
            "dyc_target_folders": len(main.DEFAULT_MVP_FOLDER_SPECS),
            "by_ownership": {"dyc_managed": len(main.DEFAULT_MVP_FOLDER_SPECS)},
            "expected_dyc_target_count": len(main.DEFAULT_MVP_FOLDER_SPECS),
            "is_bootstrapped": True,
        }

    monkeypatch.setattr(main, "_summarize_folder_inventory", fake_summarize)
    test_client = TestClient(main.app)

    response = test_client.get(
        "/alerts",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    codes = {item["code"] for item in payload["alerts"]}
    # The pending-instrumentation note is always present, but the active-state
    # codes should all be absent when the system is fully configured.
    assert "no_connected_accounts" not in codes
    assert "mailbox_access_not_ready" not in codes
    assert "runtime_config_missing" not in codes
    assert "database_unavailable" not in codes
    assert "folder_inventory_missing" not in codes
    assert "folder_inventory_incomplete" not in codes
    assert "move_worker_pending" in codes


# =============================================================================
# Message-sync instrumentation slice
# =============================================================================


def test_truncate_subject_clips_to_limit():
    short = "Hello world"
    assert main._truncate_subject(short) == short
    long_value = "x" * (main.SUBJECT_PREVIEW_MAX_CHARS + 50)
    truncated = main._truncate_subject(long_value)
    assert truncated is not None
    assert len(truncated) == main.SUBJECT_PREVIEW_MAX_CHARS
    assert truncated.endswith("…")
    assert main._truncate_subject(None) is None
    assert main._truncate_subject("   ") == ""


def test_parse_graph_datetime_handles_z_suffix():
    parsed = main._parse_graph_datetime("2026-04-28T10:15:30Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert main._parse_graph_datetime(None) is None
    assert main._parse_graph_datetime("not-a-date") is None


def test_normalize_window_days_clamps_to_supported_buckets():
    assert main._normalize_window_days(None) == 7
    assert main._normalize_window_days(0) == 7
    assert main._normalize_window_days(-3) == 7
    assert main._normalize_window_days(1) == 1
    assert main._normalize_window_days(3) == 7
    assert main._normalize_window_days(7) == 7
    assert main._normalize_window_days(15) == 30
    assert main._normalize_window_days(90) == 30


def test_record_sync_event_no_op_without_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Should not raise, should not touch psycopg.
    main._record_sync_event(
        "account-123",
        "messages.sync",
        status="success",
        started_at=main._utcnow(),
        messages_seen=3,
    )


def test_messages_sync_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    test_client = TestClient(main.app)
    response = test_client.post("/mail/messages/sync")
    assert response.status_code == 401
    assert response.json()["detail"] == "No linked account session found."


def test_messages_sync_persists_and_records_sync_event(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())

    async def fake_token(email):
        assert email == "daniel@danielyoung.io"
        return "graph-token", {
            "account_id": "account-123",
            "email": email,
            "display_name": "Daniel Young",
        }

    captured_messages: list[list[dict]] = []
    sync_events: list[dict] = []

    async def fake_list_recent_messages(access_token, folder_id, limit):
        assert access_token == "graph-token"
        assert folder_id is None
        assert limit == 50
        return [
            {
                "id": "msg-1",
                "parentFolderId": "inbox-id",
                "subject": "Hello",
                "receivedDateTime": "2026-04-28T08:30:00Z",
                "isRead": False,
                "hasAttachments": False,
            },
            {
                "id": "msg-2",
                "parentFolderId": "inbox-id",
                "subject": "World",
                "receivedDateTime": "2026-04-28T09:00:00Z",
                "isRead": True,
                "hasAttachments": True,
            },
        ]

    def fake_persist(account_id, messages):
        assert account_id == "account-123"
        captured_messages.append(messages)
        return len(messages), len(messages)

    def fake_record(account_id, operation, **kwargs):
        sync_events.append({"account_id": account_id, "operation": operation, **kwargs})

    monkeypatch.setattr(main, "_graph_access_token_for_email", fake_token)
    monkeypatch.setattr(main, "_list_recent_messages", fake_list_recent_messages)
    monkeypatch.setattr(main, "_persist_message_sightings", fake_persist)
    monkeypatch.setattr(main, "_record_sync_event", fake_record)

    test_client = TestClient(main.app)
    response = test_client.post(
        "/mail/messages/sync",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["messages_seen"] == 2
    assert body["messages_persisted"] == 2
    assert body["scope"]["folder_id"] is None
    assert len(captured_messages) == 1
    assert len(sync_events) == 1
    event = sync_events[0]
    assert event["operation"] == "messages.sync"
    assert event["status"] == "success"
    assert event["messages_seen"] == 2
    assert event["messages_persisted"] == 2


def test_messages_sync_records_error_and_reraises(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())

    async def fake_token(email):
        return "graph-token", {
            "account_id": "account-123",
            "email": email,
            "display_name": "Daniel Young",
        }

    async def boom(access_token, folder_id, limit):
        from fastapi import HTTPException

        raise HTTPException(status_code=502, detail="Graph blew up")

    sync_events: list[dict] = []

    monkeypatch.setattr(main, "_graph_access_token_for_email", fake_token)
    monkeypatch.setattr(main, "_list_recent_messages", boom)
    monkeypatch.setattr(
        main,
        "_record_sync_event",
        lambda account_id, operation, **kwargs: sync_events.append(
            {"account_id": account_id, "operation": operation, **kwargs}
        ),
    )

    test_client = TestClient(main.app)
    response = test_client.post(
        "/mail/messages/sync",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 502
    assert sync_events
    assert sync_events[0]["status"] == "error"
    assert sync_events[0]["operation"] == "messages.sync"
    assert sync_events[0]["errors"] == 1


def test_bootstrap_records_sync_event(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())

    async def fake_token(email):
        return "graph-token", {
            "account_id": "account-123",
            "email": email,
            "display_name": "Daniel Young",
        }

    async def fake_ensure(access_token):
        return [{"id": "review-id", "displayName": "10 - Review"}]

    sync_events: list[dict] = []
    monkeypatch.setattr(main, "_graph_access_token_for_email", fake_token)
    monkeypatch.setattr(main, "_ensure_default_mail_folders", fake_ensure)
    monkeypatch.setattr(main, "_persist_folder_inventory", lambda *args, **kw: None)
    monkeypatch.setattr(
        main,
        "_record_sync_event",
        lambda account_id, operation, **kwargs: sync_events.append(
            {"operation": operation, **kwargs}
        ),
    )

    test_client = TestClient(main.app)
    response = test_client.post(
        "/mail/folders/bootstrap",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    assert sync_events
    assert sync_events[0]["operation"] == "folder.bootstrap"
    assert sync_events[0]["status"] == "success"
    assert sync_events[0]["folders_seen"] == 1


def test_dashboard_summary_uses_window_filter(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: [
            {
                "account_id": "account-123",
                "provider": main.MICROSOFT_PROVIDER,
                "email": email,
                "display_name": "Daniel Young",
                "status": "active",
                "has_refresh_token": True,
                "token_updated_at": None,
                "updated_at": None,
                "created_at": None,
            }
        ],
    )
    monkeypatch.setattr(main, "_load_folder_inventory", lambda account_id: [])

    captured_windows: list[int] = []

    def fake_volume(account_id, window_days):
        captured_windows.append(window_days)
        return {
            "available": True,
            "reason": None,
            "window_days": window_days,
            "messages_in": 5,
            "messages_persisted": 5,
            "errors": 0,
            "last_message_received_at": None,
            "last_sync_at": "2026-04-28T08:00:00+00:00",
            "last_sync_status": "success",
            "last_sync_error": None,
            "by_day": [{"day": "2026-04-28", "messages_in": 5}],
            "by_folder": [{"folder": "Inbox", "messages_in": 5}],
        }

    monkeypatch.setattr(main, "_load_volume_metrics", fake_volume)
    monkeypatch.setattr(
        main,
        "_load_action_metrics",
        lambda account_id, window_days: main._empty_action_metrics(window_days),
    )
    test_client = TestClient(main.app)

    response = test_client.get(
        "/dashboard/summary?window_days=1",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_days"] == 1
    # Free-form window_days values are normalized into the 1/7/30 buckets so
    # the worker code paths only deal with three windows.
    assert captured_windows == [1]
    totals = payload["totals"]
    assert totals["messages_in"] == 5
    assert totals["messages_persisted"] == 5
    assert payload["accounts"][0]["email_volume"]["available"] is True


def test_activity_includes_sync_events(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: [
            {
                "account_id": "account-123",
                "provider": main.MICROSOFT_PROVIDER,
                "email": email,
                "display_name": "Daniel Young",
                "status": "active",
                "has_refresh_token": True,
                "token_updated_at": None,
                "updated_at": None,
                "created_at": None,
            }
        ],
    )
    monkeypatch.setattr(main, "_load_folder_activity", lambda account_id, limit: [])
    monkeypatch.setattr(
        main,
        "_load_recent_sync_events",
        lambda account_id, window_days, limit=50: [
            {
                "operation": "messages.sync",
                "status": "success",
                "folders_seen": None,
                "messages_seen": 12,
                "messages_persisted": 12,
                "errors": 0,
                "error_message": None,
                "started_at": "2026-04-28T08:00:00+00:00",
                "occurred_at": "2026-04-28T08:01:00+00:00",
            },
            {
                "operation": "folder.bootstrap",
                "status": "success",
                "folders_seen": 9,
                "messages_seen": None,
                "messages_persisted": None,
                "errors": 0,
                "error_message": None,
                "started_at": "2026-04-27T10:00:00+00:00",
                "occurred_at": "2026-04-27T10:00:01+00:00",
            },
        ],
    )
    monkeypatch.setattr(main, "_load_message_movement_events", lambda *args, **kw: [])
    test_client = TestClient(main.app)

    response = test_client.get(
        "/activity?window_days=7",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sync_activity"]["available"] is True
    operations = [event["operation"] for event in payload["sync_activity"]["events"]]
    assert operations == ["messages.sync", "folder.bootstrap"]
    assert payload["sync_activity"]["events"][0]["account"]["email"] == "daniel@danielyoung.io"
    assert payload["message_movement"]["available"] is False


def test_alerts_flag_no_message_sync_yet(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    for var in (
        "MICROSOFT_ENTRA_CLIENT_ID",
        "MICROSOFT_ENTRA_TENANT_ID",
        "MICROSOFT_ENTRA_CLIENT_SECRET",
        "MICROSOFT_ENTRA_REDIRECT_URI",
    ):
        monkeypatch.setenv(var, "x")
    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: [
            {
                "account_id": "account-123",
                "provider": main.MICROSOFT_PROVIDER,
                "email": email,
                "display_name": "Daniel Young",
                "status": "active",
                "has_refresh_token": True,
                "token_updated_at": None,
                "updated_at": None,
                "created_at": None,
            }
        ],
    )
    monkeypatch.setattr(
        main,
        "_summarize_folder_inventory",
        lambda account_id: {
            "available": True,
            "total_folders": len(main.DEFAULT_MVP_FOLDER_SPECS),
            "dyc_target_folders": len(main.DEFAULT_MVP_FOLDER_SPECS),
            "by_ownership": {"dyc_managed": len(main.DEFAULT_MVP_FOLDER_SPECS)},
            "expected_dyc_target_count": len(main.DEFAULT_MVP_FOLDER_SPECS),
            "is_bootstrapped": True,
        },
    )
    monkeypatch.setattr(
        main,
        "_load_volume_metrics",
        lambda account_id, window_days: main._empty_volume_metrics(window_days),
    )
    test_client = TestClient(main.app)

    response = test_client.get(
        "/alerts",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    codes = {item["code"] for item in response.json()["alerts"]}
    assert "no_message_sync_yet" in codes
    assert "move_worker_pending" in codes


def test_alerts_flag_recent_message_sync_error(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    for var in (
        "MICROSOFT_ENTRA_CLIENT_ID",
        "MICROSOFT_ENTRA_TENANT_ID",
        "MICROSOFT_ENTRA_CLIENT_SECRET",
        "MICROSOFT_ENTRA_REDIRECT_URI",
    ):
        monkeypatch.setenv(var, "x")
    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: [
            {
                "account_id": "account-123",
                "provider": main.MICROSOFT_PROVIDER,
                "email": email,
                "display_name": "Daniel Young",
                "status": "active",
                "has_refresh_token": True,
                "token_updated_at": None,
                "updated_at": None,
                "created_at": None,
            }
        ],
    )
    monkeypatch.setattr(
        main,
        "_summarize_folder_inventory",
        lambda account_id: {
            "available": True,
            "total_folders": len(main.DEFAULT_MVP_FOLDER_SPECS),
            "dyc_target_folders": len(main.DEFAULT_MVP_FOLDER_SPECS),
            "by_ownership": {"dyc_managed": len(main.DEFAULT_MVP_FOLDER_SPECS)},
            "expected_dyc_target_count": len(main.DEFAULT_MVP_FOLDER_SPECS),
            "is_bootstrapped": True,
        },
    )
    # A recent sync that ended in failure with an explicit error.
    monkeypatch.setattr(
        main,
        "_load_volume_metrics",
        lambda account_id, window_days: {
            "available": True,
            "reason": None,
            "window_days": window_days,
            "messages_in": 0,
            "messages_persisted": 0,
            "errors": 1,
            "last_message_received_at": None,
            "last_sync_at": main._utcnow().isoformat(),
            "last_sync_status": "error",
            "last_sync_error": "Graph 502",
            "by_day": [],
            "by_folder": [],
        },
    )
    test_client = TestClient(main.app)

    response = test_client.get(
        "/alerts",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    codes = {item["code"] for item in response.json()["alerts"]}
    assert "recent_message_sync_error" in codes


def test_messages_sync_cli_subcommand_parses():
    from apps.api.app import cli

    parser = cli.build_parser()
    args = parser.parse_args(["messages-sync", "--folder-id", "abc", "--limit", "25"])
    assert args.func is cli.cmd_messages_sync
    assert args.folder_id == "abc"
    assert args.limit == 25
