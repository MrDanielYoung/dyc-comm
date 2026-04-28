import base64
import json
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
    "ALLOWED_MICROSOFT_TENANT_IDS",
    "ALLOWED_ACCOUNT_EMAILS",
)

SECRET_VARIABLES = {"DATABASE_URL", "MICROSOFT_ENTRA_CLIENT_SECRET"}

DECODING_OPTIONS_TENANT = "99c0f350-71bd-47f9-ab6a-cf10bc76533a"
DHW_TENANT = "3dd54b52-c31e-442e-8705-a56b839e59a7"
UNKNOWN_TENANT = "deadbeef-dead-beef-dead-beefdeadbeef"


def _id_token(claims: dict) -> str:
    """Build a JWT-shaped id_token. Signature is unused — the API decodes
    the payload only, after a successful confidential-client exchange."""

    def _b64(value: dict | bytes) -> str:
        if isinstance(value, dict):
            value = json.dumps(value).encode("utf-8")
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    header = _b64({"alg": "RS256", "typ": "JWT"})
    body = _b64(claims)
    return f"{header}.{body}.signature-not-verified"


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
    monkeypatch.setenv("ALLOWED_MICROSOFT_TENANT_IDS", "tenant-a,tenant-b")
    monkeypatch.setenv("ALLOWED_ACCOUNT_EMAILS", "a@example.com")

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
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", DECODING_OPTIONS_TENANT)
    persisted_profiles: list[dict[str, str]] = []

    id_token = _id_token(
        {"tid": DECODING_OPTIONS_TENANT, "preferred_username": "daniel@danielyoung.io"}
    )
    token_payload = {"access_token": "token", "id_token": id_token}

    async def fake_exchange(code: str, verifier: str):
        assert code == "sample-code"
        assert verifier == "stored-verifier"
        return token_payload

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
            "token_response": token_payload,
        }
    ]
    assert main.EMAIL_COOKIE in response.cookies
    assert main.NAME_COOKIE in response.cookies


def _wire_callback(
    monkeypatch,
    *,
    tid: str,
    email: str,
    display_name: str = "Daniel Young",
):
    """Wire the callback handler with a synthetic token + Graph profile and
    return a list that captures any persistence calls. The list must remain
    empty when the allow-list rejects the sign-in."""
    persisted: list[dict] = []

    async def fake_exchange(code, verifier):
        return {
            "access_token": "token",
            "id_token": _id_token({"tid": tid, "preferred_username": email}),
        }

    async def fake_profile(access_token):
        return {"mail": email, "displayName": display_name}

    def fake_persist(profile, token_response):
        persisted.append({"profile": profile, "token_response": token_response})
        return {
            "email": email,
            "display_name": display_name,
            "provider_account_id": "account-id",
            "has_refresh_token": True,
        }

    monkeypatch.setattr(main, "_exchange_code", fake_exchange)
    monkeypatch.setattr(main, "_graph_profile", fake_profile)
    monkeypatch.setattr(main, "_persist_microsoft_account", fake_persist)
    return persisted


def test_callback_allows_home_tenant_when_only_home_configured(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", DECODING_OPTIONS_TENANT)
    monkeypatch.delenv("ALLOWED_MICROSOFT_TENANT_IDS", raising=False)
    monkeypatch.delenv("ALLOWED_ACCOUNT_EMAILS", raising=False)
    persisted = _wire_callback(
        monkeypatch, tid=DECODING_OPTIONS_TENANT, email="daniel@danielyoung.io"
    )

    test_client = TestClient(main.app)
    response = test_client.get(
        "/auth/microsoft/callback?code=c&state=s",
        cookies={main.AUTH_COOKIE: "s", main.PKCE_COOKIE: "v"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "auth=success" in response.headers["location"]
    assert len(persisted) == 1


def test_callback_allows_dhw_tenant_when_allow_listed(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", DECODING_OPTIONS_TENANT)
    monkeypatch.setenv(
        "ALLOWED_MICROSOFT_TENANT_IDS", f"{DECODING_OPTIONS_TENANT},{DHW_TENANT}"
    )
    monkeypatch.setenv(
        "ALLOWED_ACCOUNT_EMAILS",
        "daniel@danielyoung.io,daniel.young@digitalhealthworks.com",
    )
    persisted = _wire_callback(
        monkeypatch,
        tid=DHW_TENANT,
        email="daniel.young@digitalhealthworks.com",
    )

    test_client = TestClient(main.app)
    response = test_client.get(
        "/auth/microsoft/callback?code=c&state=s",
        cookies={main.AUTH_COOKIE: "s", main.PKCE_COOKIE: "v"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "auth=success" in response.headers["location"]
    assert len(persisted) == 1


def test_callback_rejects_unknown_tenant_and_does_not_persist(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", DECODING_OPTIONS_TENANT)
    monkeypatch.setenv(
        "ALLOWED_MICROSOFT_TENANT_IDS", f"{DECODING_OPTIONS_TENANT},{DHW_TENANT}"
    )
    persisted = _wire_callback(
        monkeypatch, tid=UNKNOWN_TENANT, email="stranger@otherco.com"
    )

    test_client = TestClient(main.app)
    response = test_client.get(
        "/auth/microsoft/callback?code=c&state=s",
        cookies={main.AUTH_COOKIE: "s", main.PKCE_COOKIE: "v"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "tenant" in response.json()["detail"].lower()
    assert persisted == []
    assert main.EMAIL_COOKIE not in response.cookies


def test_callback_rejects_external_tenant_when_only_home_configured(monkeypatch):
    """Default-deny posture: no env list ⇒ only MICROSOFT_ENTRA_TENANT_ID."""
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", DECODING_OPTIONS_TENANT)
    monkeypatch.delenv("ALLOWED_MICROSOFT_TENANT_IDS", raising=False)
    persisted = _wire_callback(
        monkeypatch, tid=DHW_TENANT, email="daniel.young@digitalhealthworks.com"
    )

    test_client = TestClient(main.app)
    response = test_client.get(
        "/auth/microsoft/callback?code=c&state=s",
        cookies={main.AUTH_COOKIE: "s", main.PKCE_COOKIE: "v"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert persisted == []


def test_callback_rejects_email_not_in_allow_list(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", DECODING_OPTIONS_TENANT)
    monkeypatch.setenv(
        "ALLOWED_MICROSOFT_TENANT_IDS", f"{DECODING_OPTIONS_TENANT},{DHW_TENANT}"
    )
    monkeypatch.setenv(
        "ALLOWED_ACCOUNT_EMAILS", "daniel@danielyoung.io"
    )
    persisted = _wire_callback(
        monkeypatch, tid=DHW_TENANT, email="someone-else@digitalhealthworks.com"
    )

    test_client = TestClient(main.app)
    response = test_client.get(
        "/auth/microsoft/callback?code=c&state=s",
        cookies={main.AUTH_COOKIE: "s", main.PKCE_COOKIE: "v"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "account" in response.json()["detail"].lower()
    assert persisted == []


def test_callback_rejects_when_no_tenant_allow_list_can_be_resolved(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.delenv("MICROSOFT_ENTRA_TENANT_ID", raising=False)
    monkeypatch.delenv("ALLOWED_MICROSOFT_TENANT_IDS", raising=False)
    persisted = _wire_callback(
        monkeypatch, tid=DECODING_OPTIONS_TENANT, email="daniel@danielyoung.io"
    )

    test_client = TestClient(main.app)
    response = test_client.get(
        "/auth/microsoft/callback?code=c&state=s",
        cookies={main.AUTH_COOKIE: "s", main.PKCE_COOKIE: "v"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert persisted == []


def test_callback_rejects_when_id_token_lacks_tid(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", DECODING_OPTIONS_TENANT)
    persisted: list[dict] = []

    async def fake_exchange(code, verifier):
        # No id_token at all, so tid cannot be established.
        return {"access_token": "token"}

    async def fake_profile(access_token):
        return {"mail": "daniel@danielyoung.io", "displayName": "Daniel Young"}

    def fake_persist(profile, token_response):
        persisted.append({"profile": profile, "token_response": token_response})
        return {
            "email": "daniel@danielyoung.io",
            "display_name": "Daniel Young",
            "provider_account_id": "x",
            "has_refresh_token": True,
        }

    monkeypatch.setattr(main, "_exchange_code", fake_exchange)
    monkeypatch.setattr(main, "_graph_profile", fake_profile)
    monkeypatch.setattr(main, "_persist_microsoft_account", fake_persist)

    test_client = TestClient(main.app)
    response = test_client.get(
        "/auth/microsoft/callback?code=c&state=s",
        cookies={main.AUTH_COOKIE: "s", main.PKCE_COOKIE: "v"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "tid" in response.json()["detail"].lower()
    assert persisted == []


def test_authorize_url_uses_organizations_when_multi_tenant(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", DECODING_OPTIONS_TENANT)
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_ID", "client-id")
    monkeypatch.setenv(
        "MICROSOFT_ENTRA_REDIRECT_URI",
        "http://localhost:8000/auth/microsoft/callback",
    )
    monkeypatch.setenv(
        "ALLOWED_MICROSOFT_TENANT_IDS", f"{DECODING_OPTIONS_TENANT},{DHW_TENANT}"
    )
    test_client = TestClient(main.app)

    response = test_client.get("/auth/microsoft/start", follow_redirects=False)

    assert response.status_code == 302
    parsed = urlparse(response.headers["location"])
    assert parsed.path.startswith("/organizations/")


def test_authorize_url_pins_home_tenant_when_only_home_allowed(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", DECODING_OPTIONS_TENANT)
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_ID", "client-id")
    monkeypatch.setenv(
        "MICROSOFT_ENTRA_REDIRECT_URI",
        "http://localhost:8000/auth/microsoft/callback",
    )
    monkeypatch.delenv("ALLOWED_MICROSOFT_TENANT_IDS", raising=False)
    test_client = TestClient(main.app)

    response = test_client.get("/auth/microsoft/start", follow_redirects=False)

    assert response.status_code == 302
    parsed = urlparse(response.headers["location"])
    assert parsed.path.startswith(f"/{DECODING_OPTIONS_TENANT}/")


def test_config_check_reports_allow_list_presence_without_values(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", DECODING_OPTIONS_TENANT)
    monkeypatch.setenv(
        "ALLOWED_MICROSOFT_TENANT_IDS", f"{DECODING_OPTIONS_TENANT},{DHW_TENANT}"
    )
    monkeypatch.setenv(
        "ALLOWED_ACCOUNT_EMAILS",
        "daniel@danielyoung.io,daniel.young@digitalhealthworks.com",
    )
    test_client = TestClient(main.app)

    response = test_client.get("/config-check")

    assert response.status_code == 200
    payload = response.json()
    allow_list = payload["auth_allow_list"]
    assert allow_list["tenant_allow_list_configured"] is True
    assert allow_list["tenant_allow_list_count"] == 2
    assert allow_list["email_allow_list_configured"] is True
    assert allow_list["email_allow_list_count"] == 2
    assert allow_list["multi_tenant_authorize"] is True

    body = response.text
    for value in (
        DHW_TENANT,
        "daniel@danielyoung.io",
        "daniel.young@digitalhealthworks.com",
    ):
        assert value not in body, f"allow-list value leaked: {value!r}"

    variables = payload["variables"]
    assert variables["ALLOWED_MICROSOFT_TENANT_IDS"]["present"] is True
    assert variables["ALLOWED_MICROSOFT_TENANT_IDS"]["is_secret"] is False
    assert variables["ALLOWED_ACCOUNT_EMAILS"]["present"] is True
    assert variables["ALLOWED_ACCOUNT_EMAILS"]["is_secret"] is False


def test_config_check_all_required_present_ignores_optional_allow_list(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_ID", "client-id")
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", DECODING_OPTIONS_TENANT)
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_SECRET", "secret")
    monkeypatch.setenv(
        "MICROSOFT_ENTRA_REDIRECT_URI",
        "http://localhost:8000/auth/microsoft/callback",
    )
    monkeypatch.setenv("KEY_VAULT_REFS_ENABLED", "false")
    monkeypatch.delenv("ALLOWED_MICROSOFT_TENANT_IDS", raising=False)
    monkeypatch.delenv("ALLOWED_ACCOUNT_EMAILS", raising=False)
    test_client = TestClient(main.app)

    response = test_client.get("/config-check")

    payload = response.json()
    assert payload["all_required_present"] is True
    assert payload["auth_allow_list"]["tenant_allow_list_configured"] is False
    assert payload["auth_allow_list"]["tenant_allow_list_count"] == 1  # home tenant fallback
    assert payload["auth_allow_list"]["email_allow_list_configured"] is False


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
    assert payload["totals"] == {
        "connected_accounts": 1,
        "mailbox_ready_accounts": 0,
        "total_folders": 0,
        "dyc_target_folders": 0,
    }
    assert payload["accounts"][0]["account"]["status"] == "session_only"
    assert payload["accounts"][0]["folder_inventory"]["available"] is False
    assert payload["accounts"][0]["email_volume"]["available"] is False
    assert payload["accounts"][0]["action_activity"]["available"] is False
    pending = {entry["metric"] for entry in payload["pending_instrumentation"]}
    assert pending == {"email_volume", "action_activity"}


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
    assert account_entry["email_volume"]["messages_in"] is None
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
    assert "activity_instrumentation_pending" in codes
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
    assert "activity_instrumentation_pending" in codes


def _two_account_rows(user_email):
    return [
        {
            "account_id": "account-personal",
            "provider": main.MICROSOFT_PROVIDER,
            "email": user_email,
            "display_name": "Daniel Young",
            "status": "active",
            "has_refresh_token": True,
            "token_updated_at": None,
            "updated_at": None,
            "created_at": None,
        },
        {
            "account_id": "account-dhw",
            "provider": main.MICROSOFT_PROVIDER,
            "email": "daniel.young@digitalhealthworks.com",
            "display_name": "Daniel Young (DHW)",
            "status": "active",
            "has_refresh_token": True,
            "token_updated_at": None,
            "updated_at": None,
            "created_at": None,
        },
    ]


def test_activity_filters_by_account_query(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: _two_account_rows(email),
    )

    captured: list[str] = []

    def fake_load_folder_activity(account_id, limit):
        captured.append(account_id)
        return [
            {
                "event_type": "folder.bootstrap",
                "occurred_at": "2026-04-28T10:00:00+00:00",
                "folder": {
                    "provider_folder_id": f"{account_id}-folder",
                    "display_name": "10 - Review",
                    "canonical_name": "10 - Review",
                    "ownership": "dyc_managed",
                    "is_dyc_target": True,
                },
            }
        ]

    monkeypatch.setattr(main, "_load_folder_activity", fake_load_folder_activity)
    test_client = TestClient(main.app)

    response = test_client.get(
        "/activity",
        params={"account": "daniel.young@digitalhealthworks.com"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == {"account": "daniel.young@digitalhealthworks.com"}
    assert captured == ["account-dhw"]
    assert all(
        event["account"]["email"] == "daniel.young@digitalhealthworks.com"
        for event in payload["folder_activity"]["events"]
    )


def test_activity_returns_404_for_unknown_account_query(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: _two_account_rows(email),
    )
    test_client = TestClient(main.app)

    response = test_client.get(
        "/activity",
        params={"account": "stranger@example.com"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 404


def test_alerts_filters_by_account_query(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    for var in (
        "MICROSOFT_ENTRA_CLIENT_ID",
        "MICROSOFT_ENTRA_TENANT_ID",
        "MICROSOFT_ENTRA_CLIENT_SECRET",
        "MICROSOFT_ENTRA_REDIRECT_URI",
    ):
        monkeypatch.setenv(var, "x")

    rows = _two_account_rows("daniel@danielyoung.io")
    rows[0]["has_refresh_token"] = False  # personal mailbox unhealthy
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: rows)
    monkeypatch.setattr(
        main,
        "_summarize_folder_inventory",
        lambda account_id: {
            "available": True,
            "total_folders": len(main.DEFAULT_MVP_FOLDER_SPECS) + 1,
            "dyc_target_folders": len(main.DEFAULT_MVP_FOLDER_SPECS),
            "by_ownership": {"dyc_managed": len(main.DEFAULT_MVP_FOLDER_SPECS)},
            "expected_dyc_target_count": len(main.DEFAULT_MVP_FOLDER_SPECS),
            "is_bootstrapped": True,
        },
    )
    test_client = TestClient(main.app)

    # Scoped to the healthy DHW mailbox: no mailbox-not-ready alert should appear.
    response = test_client.get(
        "/alerts",
        params={"account": "daniel.young@digitalhealthworks.com"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == {"account": "daniel.young@digitalhealthworks.com"}
    codes = {item["code"] for item in payload["alerts"]}
    assert "mailbox_access_not_ready" not in codes

    # Scoped to the unhealthy personal mailbox: alert is present and references it.
    response = test_client.get(
        "/alerts",
        params={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )
    assert response.status_code == 200
    items = response.json()["alerts"]
    not_ready = [item for item in items if item["code"] == "mailbox_access_not_ready"]
    assert len(not_ready) == 1
    assert not_ready[0]["context"]["email"] == "daniel@danielyoung.io"


def test_alerts_returns_404_for_unknown_account_query(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(
        main,
        "_list_user_accounts",
        lambda email: _two_account_rows(email),
    )
    test_client = TestClient(main.app)

    response = test_client.get(
        "/alerts",
        params={"account": "stranger@example.com"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 404
