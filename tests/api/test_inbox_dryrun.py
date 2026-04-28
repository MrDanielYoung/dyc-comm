"""Focused tests for the inbox dry-run classification endpoints.

These tests exercise:

* Auth: an unauthenticated session is rejected.
* Account scoping: a session can only run the dry-run against an account
  email it actually has linked.
* Graph behavior: only a single read-only GET is issued per call (no
  POST/PATCH/DELETE — the runtime never mutates the mailbox).
* Persistence: each classified message produces a row via the persistence
  hook, and the read endpoint returns those rows back.
* Provider fallback: when Azure OpenAI/AI env vars are absent the
  deterministic classifier still runs and the response is marked
  ``provider_consulted: False`` with ``review_folder = '10 - Review'``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import classifier as classifier_module
from apps.api.app import main


def _local_settings() -> main.Settings:
    return main.Settings(
        app_env="local",
        web_app_url="http://localhost:3000",
        api_base_url="http://localhost:8000",
        allowed_origins=["http://localhost:3000"],
        key_vault_refs_enabled=False,
        legacy_rule_folder_names=("Wolt",),
    )


def _account_row(email: str = "daniel@danielyoung.io") -> dict:
    return {
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


def _clear_ai_env(monkeypatch):
    for var in (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_API_KEY",
        "AZURE_AI_ENDPOINT",
        "AZURE_AI_DEPLOYMENT",
        "AZURE_AI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_classify_inbox_dryrun_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun",
        params={"account": "daniel@danielyoung.io"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "No linked account session found."


def test_classify_inbox_dryrun_rejects_account_not_linked(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])
    client = TestClient(main.app)

    response = client.post(
        "/mail/inbox/classify-dryrun",
        params={"account": "stranger@example.com"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 404
    assert "is linked" in response.json()["detail"].lower()


def test_classify_inbox_dryrun_uses_only_read_only_graph_calls(monkeypatch):
    """Tracks every Graph call and asserts none of them are writes."""
    monkeypatch.setattr(main, "settings", _local_settings())
    _clear_ai_env(monkeypatch)
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])

    async def fake_token(email):
        return "graph-token", {
            "account_id": "account-123",
            "email": email,
            "display_name": "Daniel Young",
        }

    monkeypatch.setattr(main, "_graph_access_token_for_email", fake_token)

    graph_calls: list[tuple[str, str, dict | None]] = []

    async def fake_graph_get(token, path, params=None):
        graph_calls.append(("GET", path, params))
        return {
            "value": [
                {
                    "id": "msg-1",
                    "receivedDateTime": "2026-04-28T10:00:00Z",
                    "subject": "Welcome to the news",
                    "bodyPreview": (
                        "Subscribe to our weekly newsletter for product updates "
                        "and industry news every Tuesday."
                    ),
                    "from": {"emailAddress": {"address": "news@example.com"}},
                    "parentFolderId": "inbox-folder-id",
                },
                {
                    "id": "msg-2",
                    "receivedDateTime": "2026-04-28T09:00:00Z",
                    "subject": "Quick question",
                    "bodyPreview": "Hey",  # too short — must force review
                    "from": {"emailAddress": {"address": "alex@example.com"}},
                    "parentFolderId": "inbox-folder-id",
                },
            ]
        }

    async def fail_graph_post(*args, **kwargs):
        graph_calls.append(("POST", args[1] if len(args) > 1 else "?", None))
        raise AssertionError("dry-run must not POST to Microsoft Graph")

    monkeypatch.setattr(main, "_graph_get", fake_graph_get)
    monkeypatch.setattr(main, "_graph_post", fail_graph_post)

    persisted: list[dict] = []

    def fake_persist(**kwargs):
        persisted.append(kwargs)

    monkeypatch.setattr(main, "_persist_dry_run_classification", fake_persist)

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun",
        params={"account": "daniel@danielyoung.io", "limit": 5},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["destructive"] is False
    assert payload["fetched"] == 2
    assert payload["classified"] == 2
    assert payload["errors"] == 0
    assert payload["review_folder"] == classifier_module.REVIEW_FOLDER
    assert payload["provider"]["consulted"] is False
    assert payload["provider"]["selected"] == "none"
    assert payload["provider"]["configured"] is False

    # Only one read-only Graph call should have been issued, against the
    # inbox messages collection.
    assert len(graph_calls) == 1
    method, path, params = graph_calls[0]
    assert method == "GET"
    assert path == "/me/mailFolders/inbox/messages"
    assert params is not None
    assert params["$top"] == "5"
    assert params["$orderby"] == "receivedDateTime desc"

    # Both messages should land in the persistence hook.
    assert len(persisted) == 2
    persisted_ids = {entry["message"].get("id") for entry in persisted}
    assert persisted_ids == {"msg-1", "msg-2"}
    for entry in persisted:
        # provider_consulted is always False on the deterministic path.
        assert entry["decision"].provider_consulted is False
        assert entry["account_id"] == "account-123"
        assert entry["account_email"] == "daniel@danielyoung.io"
        assert entry["status"] == "classified"

    # The short message must be forced to 10 - Review by the safety pass.
    short = next(r for r in payload["results"] if r["provider_message_id"] == "msg-2")
    assert short["recommendation"]["recommended_folder"] == classifier_module.REVIEW_FOLDER
    assert short["recommendation"]["forced_review"] is True
    assert short["provider_consulted"] is False


def test_classify_inbox_dryrun_log_returns_persisted_rows(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])

    captured: list[tuple[str, int]] = []

    def fake_load(account_id, limit):
        captured.append((account_id, limit))
        return [
            {
                "provider_message_id": "msg-1",
                "account_email": "daniel@danielyoung.io",
                "received_at": "2026-04-28T10:00:00+00:00",
                "sender": "news@example.com",
                "subject": "Welcome to the news",
                "current_folder": "inbox-folder-id",
                "recommended_folder": classifier_module.REVIEW_FOLDER,
                "category": "unknown_ambiguous",
                "confidence": 0.0,
                "confidence_band": "low",
                "forced_review": True,
                "reasons": ["confidence below medium threshold — defaulting to 10 - Review"],
                "safety_flags": [],
                "provider_consulted": False,
                "provider": "none",
                "status": "classified",
                "error": None,
                "created_at": "2026-04-28T10:00:01+00:00",
            }
        ]

    monkeypatch.setattr(main, "_load_dry_run_log", fake_load)
    client = TestClient(main.app)

    response = client.get(
        "/mail/inbox/classify-dryrun/log",
        params={"account": "daniel@danielyoung.io", "limit": 10},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert captured == [("account-123", 10)]
    assert payload["account"] == {
        "email": "daniel@danielyoung.io",
        "display_name": "Daniel Young",
    }
    assert payload["count"] == 1
    assert payload["entries"][0]["provider_message_id"] == "msg-1"
    assert payload["entries"][0]["recommended_folder"] == classifier_module.REVIEW_FOLDER
    assert payload["review_folder"] == classifier_module.REVIEW_FOLDER


def test_classify_inbox_dryrun_log_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    client = TestClient(main.app)
    response = client.get(
        "/mail/inbox/classify-dryrun/log",
        params={"account": "daniel@danielyoung.io"},
    )
    assert response.status_code == 401


def test_classify_inbox_dryrun_log_rejects_unscoped_account(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])
    client = TestClient(main.app)

    response = client.get(
        "/mail/inbox/classify-dryrun/log",
        params={"account": "stranger@example.com"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 404


def test_cli_inbox_dryrun_subcommands_dispatch():
    from apps.api.app import cli

    parser = cli.build_parser()

    args = parser.parse_args(["inbox-dryrun"])
    assert args.func is cli.cmd_inbox_dryrun
    assert args.account == "daniel@danielyoung.io"
    assert args.limit == 25

    args = parser.parse_args(["inbox-dryrun", "--account", "x@example.com", "--limit", "5"])
    assert args.account == "x@example.com"
    assert args.limit == 5

    args = parser.parse_args(["inbox-dryrun-log"])
    assert args.func is cli.cmd_inbox_dryrun_log
    assert args.account == "daniel@danielyoung.io"
    assert args.limit == 25


def test_classify_inbox_dryrun_response_shape_matches_ui_contract(monkeypatch):
    """The web UI reads specific fields off the dry-run response — keep them stable.

    The Inbox Sorting tab renders a summary line like
    ``X classified, Y forced_review, Z errors`` and a table whose rows pull
    ``recommendation.recommended_folder``, ``recommendation.confidence``,
    ``recommendation.confidence_band``, ``recommendation.reasons``,
    ``recommendation.safety_flags``, and ``recommendation.forced_review``.
    Any rename here would silently empty the operator's view.
    """
    monkeypatch.setattr(main, "settings", _local_settings())
    _clear_ai_env(monkeypatch)
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])

    async def fake_token(email):
        return "graph-token", {
            "account_id": "account-123",
            "email": email,
            "display_name": "Daniel Young",
        }

    monkeypatch.setattr(main, "_graph_access_token_for_email", fake_token)

    async def fake_graph_get(token, path, params=None):
        return {
            "value": [
                {
                    "id": "msg-short",
                    "receivedDateTime": "2026-04-28T10:00:00Z",
                    "subject": "Hi",
                    "bodyPreview": "Hey",
                    "from": {"emailAddress": {"address": "alex@example.com"}},
                    "parentFolderId": "inbox-folder-id",
                },
            ]
        }

    monkeypatch.setattr(main, "_graph_get", fake_graph_get)
    monkeypatch.setattr(main, "_persist_dry_run_classification", lambda **kwargs: None)

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun",
        params={"account": "daniel@danielyoung.io", "limit": 1},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    # Top-level fields the UI's summary line depends on.
    expected_top_fields = (
        "dry_run",
        "destructive",
        "fetched",
        "classified",
        "errors",
        "review_folder",
        "results",
    )
    for field in expected_top_fields:
        assert field in payload, f"missing top-level field {field}"
    assert payload["destructive"] is False
    assert payload["dry_run"] is True

    # Each result row must carry the keys the table renders.
    result = payload["results"][0]
    expected_row_fields = (
        "provider_message_id",
        "received_at",
        "sender",
        "subject",
        "recommendation",
        "status",
    )
    for field in expected_row_fields:
        assert field in result, f"missing result field {field}"

    rec = result["recommendation"]
    assert rec is not None
    for field in (
        "recommended_folder",
        "category",
        "confidence",
        "confidence_band",
        "reasons",
        "safety_flags",
        "forced_review",
        "provider_consulted",
        "provider",
    ):
        assert field in rec, f"missing recommendation field {field}"

    # The short body must force review per the safety policy.
    assert rec["forced_review"] is True
    assert rec["recommended_folder"] == classifier_module.REVIEW_FOLDER


def test_protected_inbox_dryrun_endpoints_listed_in_protection_table():
    """Defense-in-depth: ensure both endpoints respond 401 with no cookie."""
    client = TestClient(main.app)
    for method, path in (
        ("POST", "/mail/inbox/classify-dryrun?account=daniel@danielyoung.io"),
        ("GET", "/mail/inbox/classify-dryrun/log?account=daniel@danielyoung.io"),
    ):
        response = client.request(method, path)
        assert response.status_code == 401, f"{method} {path} should require a session"


def test_classify_inbox_dryrun_surfaces_setup_required_when_no_refresh_token(monkeypatch):
    """If a linked account has no refresh token, the dry-run cannot fetch
    inbox messages. The API must return 409 with an actionable message that
    points the operator at re-running Microsoft sign-in. This is the
    contract the web Sorting tab relies on when it shows the
    "Reconnect this account" CTA for not-ready accounts.
    """
    monkeypatch.setattr(main, "settings", _local_settings())
    _clear_ai_env(monkeypatch)
    not_ready = _account_row()
    not_ready["has_refresh_token"] = False
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [not_ready])

    def fake_load_credentials(email):
        return {
            "account_id": "account-123",
            "provider_account_id": "provider-id",
            "email": email,
            "display_name": "Daniel Young",
            "access_token": None,
            "refresh_token": None,
            "access_token_expires_at": None,
        }

    monkeypatch.setattr(main, "_load_account_credentials", fake_load_credentials)

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun",
        params={"account": "daniel@danielyoung.io", "limit": 5},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "refresh token" in detail.lower()
    assert "sign-in" in detail.lower()


def test_accounts_endpoint_reports_mailbox_access_ready_per_account(monkeypatch):
    """The web nav reads `mailbox_access_ready` per account to decide whether
    to show the "Reconnect this account" CTA. Pin the response shape so a
    rename here doesn't silently turn the CTA off for not-ready accounts.
    """
    monkeypatch.setattr(main, "settings", _local_settings())

    primary = {
        "account_id": "account-primary",
        "provider": main.MICROSOFT_PROVIDER,
        "email": "daniel@danielyoung.io",
        "display_name": "Daniel Young",
        "status": "active",
        "has_refresh_token": True,
        "token_updated_at": None,
        "updated_at": None,
        "created_at": None,
    }
    secondary = {
        "account_id": "account-dhw",
        "provider": main.MICROSOFT_PROVIDER,
        "email": "daniel.young@digitalhealthworks.com",
        "display_name": "Daniel Young (DHW)",
        "status": "active",
        "has_refresh_token": False,
        "token_updated_at": None,
        "updated_at": None,
        "created_at": None,
    }

    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [primary, secondary])
    client = TestClient(main.app)
    response = client.get(
        "/accounts",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )
    assert response.status_code == 200
    payload = response.json()
    accounts_by_email = {entry["email"]: entry for entry in payload["accounts"]}
    primary_entry = accounts_by_email["daniel@danielyoung.io"]
    secondary_entry = accounts_by_email["daniel.young@digitalhealthworks.com"]
    assert primary_entry["mailbox_access_ready"] is True
    assert secondary_entry["mailbox_access_ready"] is False
