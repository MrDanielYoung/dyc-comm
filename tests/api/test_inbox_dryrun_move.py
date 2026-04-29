"""Tests for the human-approved dry-run move endpoint.

Exercises:

* Auth: 401 when there is no session.
* Account scoping: 404 when ``account`` is not linked to the session.
* Recommendation gate: 404 when no dry-run row exists for the message.
* Safety: a ``forced_review`` row may only be moved to ``10 - Review``,
  even if the caller asks for a different folder.
* Happy path: a normal recommendation moves the message via a single
  Graph POST to ``/me/messages/{id}/move`` and persists execution
  metadata.
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


def _dry_run_row(
    *,
    recommended_folder: str = "20 - News",
    forced_review: bool = False,
    safety_flags: list[str] | None = None,
) -> dict:
    return {
        "id": "dryrun-1",
        "account_id": "account-123",
        "account_email": "daniel@danielyoung.io",
        "provider_message_id": "msg-1",
        "recommended_folder": recommended_folder,
        "category": "newsletters_news",
        "confidence": 0.92,
        "confidence_band": "high",
        "forced_review": forced_review,
        "safety_flags": safety_flags or [],
        "status": "classified",
        "executed_at": None,
        "executed_to_folder": None,
        "executed_provider_folder_id": None,
        "executed_provider_message_id": None,
    }


async def _fake_token(email):
    return "graph-token", {
        "account_id": "account-123",
        "email": email,
        "display_name": "Daniel Young",
    }


def _patch_folder_listing(monkeypatch, folder_name: str, folder_id: str):
    async def fake_graph_get(token, path, params=None):
        assert path == "/me/mailFolders"
        return {
            "value": [
                {"id": folder_id, "displayName": folder_name},
                {"id": "other-folder-id", "displayName": "Inbox"},
            ]
        }

    monkeypatch.setattr(main, "_graph_get", fake_graph_get)


def test_move_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun/move",
        json={"account": "daniel@danielyoung.io", "provider_message_id": "msg-1"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "No linked account session found."


def test_move_rejects_unscoped_account(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])
    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun/move",
        json={"account": "stranger@example.com", "provider_message_id": "msg-1"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )
    assert response.status_code == 404
    assert "is linked" in response.json()["detail"].lower()


def test_move_requires_existing_dry_run_row(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])
    monkeypatch.setattr(main, "_load_dry_run_row", lambda account_id, message_id: None)

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun/move",
        json={"account": "daniel@danielyoung.io", "provider_message_id": "msg-missing"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )
    assert response.status_code == 404
    assert "dry-run" in response.json()["detail"].lower()


def test_move_forced_review_rejects_other_target(monkeypatch):
    """A forced_review row may only land in 10 - Review."""
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])
    monkeypatch.setattr(
        main,
        "_load_dry_run_row",
        lambda account_id, message_id: _dry_run_row(
            recommended_folder=classifier_module.REVIEW_FOLDER,
            forced_review=True,
            safety_flags=["legal_terms_detected"],
        ),
    )

    async def boom(*args, **kwargs):
        raise AssertionError("must not call Graph when target is rejected")

    monkeypatch.setattr(main, "_graph_access_token_for_email", boom)

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun/move",
        json={
            "account": "daniel@danielyoung.io",
            "provider_message_id": "msg-1",
            "target_folder": "20 - News",
        },
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )
    assert response.status_code == 409
    assert "review" in response.json()["detail"].lower()


def test_move_forced_review_routes_to_review_folder(monkeypatch):
    """When the row is forced_review and the caller omits target_folder,
    the move proceeds against 10 - Review."""
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])
    monkeypatch.setattr(
        main,
        "_load_dry_run_row",
        lambda account_id, message_id: _dry_run_row(
            recommended_folder=classifier_module.REVIEW_FOLDER,
            forced_review=True,
            safety_flags=["legal_terms_detected"],
        ),
    )
    monkeypatch.setattr(main, "_graph_access_token_for_email", _fake_token)
    _patch_folder_listing(monkeypatch, classifier_module.REVIEW_FOLDER, "review-folder-id")

    graph_writes: list[tuple[str, dict]] = []

    async def fake_graph_post(token, path, payload):
        graph_writes.append((path, payload))
        return {"id": "msg-new-1", "parentFolderId": "review-folder-id"}

    monkeypatch.setattr(main, "_graph_post", fake_graph_post)

    recorded: list[dict] = []

    def fake_record(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(main, "_record_dry_run_move", fake_record)

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun/move",
        json={"account": "daniel@danielyoung.io", "provider_message_id": "msg-1"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["moved"] is True
    assert payload["destructive"] is False
    assert payload["target_folder"] == classifier_module.REVIEW_FOLDER
    assert payload["forced_review_applied"] is True
    assert payload["new_provider_message_id"] == "msg-new-1"

    # Exactly one write to Graph, the move, against the original id.
    assert len(graph_writes) == 1
    path, body = graph_writes[0]
    assert path == "/me/messages/msg-1/move"
    assert body == {"destinationId": "review-folder-id"}

    # Persistence reflects the executed move.
    assert len(recorded) == 1
    rec = recorded[0]
    assert rec["status"] == "moved"
    assert rec["executed_to_folder"] == classifier_module.REVIEW_FOLDER
    assert rec["executed_provider_folder_id"] == "review-folder-id"
    assert rec["executed_provider_message_id"] == "msg-new-1"
    assert rec["action_error"] is None


def test_move_success_uses_recommended_folder(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])
    monkeypatch.setattr(
        main,
        "_load_dry_run_row",
        lambda account_id, message_id: _dry_run_row(recommended_folder="20 - News"),
    )
    monkeypatch.setattr(main, "_graph_access_token_for_email", _fake_token)
    _patch_folder_listing(monkeypatch, "20 - News", "news-folder-id")

    graph_writes: list[tuple[str, dict]] = []

    async def fake_graph_post(token, path, payload):
        graph_writes.append((path, payload))
        return {"id": "msg-new-news"}

    monkeypatch.setattr(main, "_graph_post", fake_graph_post)
    monkeypatch.setattr(main, "_record_dry_run_move", lambda **kwargs: None)

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun/move",
        json={"account": "daniel@danielyoung.io", "provider_message_id": "msg-1"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["moved"] is True
    assert payload["target_folder"] == "20 - News"
    assert payload["destination_provider_folder_id"] == "news-folder-id"
    assert payload["forced_review_applied"] is False

    assert len(graph_writes) == 1
    path, body = graph_writes[0]
    assert path == "/me/messages/msg-1/move"
    assert body == {"destinationId": "news-folder-id"}


def test_move_rejects_non_canonical_target(monkeypatch):
    """The destination must be a canonical DYC folder."""
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])
    monkeypatch.setattr(
        main,
        "_load_dry_run_row",
        lambda account_id, message_id: _dry_run_row(recommended_folder="20 - News"),
    )

    async def boom(*args, **kwargs):
        raise AssertionError("must not call Graph when target is not canonical")

    monkeypatch.setattr(main, "_graph_access_token_for_email", boom)

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun/move",
        json={
            "account": "daniel@danielyoung.io",
            "provider_message_id": "msg-1",
            "target_folder": "Some Random User Folder",
        },
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )
    assert response.status_code == 409
    assert "canonical" in response.json()["detail"].lower()


def test_move_requires_destination_folder_to_exist(monkeypatch):
    """If the canonical folder isn't in the mailbox yet (no bootstrap), 409."""
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])
    monkeypatch.setattr(
        main,
        "_load_dry_run_row",
        lambda account_id, message_id: _dry_run_row(recommended_folder="20 - News"),
    )
    monkeypatch.setattr(main, "_graph_access_token_for_email", _fake_token)

    async def fake_graph_get(token, path, params=None):
        return {"value": [{"id": "inbox-id", "displayName": "Inbox"}]}

    monkeypatch.setattr(main, "_graph_get", fake_graph_get)

    async def boom(*args, **kwargs):
        raise AssertionError("must not POST when destination folder is missing")

    monkeypatch.setattr(main, "_graph_post", boom)

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun/move",
        json={"account": "daniel@danielyoung.io", "provider_message_id": "msg-1"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 409
    assert "bootstrap" in response.json()["detail"].lower()


def test_move_requires_account_field(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])
    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun/move",
        json={"provider_message_id": "msg-1"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )
    assert response.status_code == 400
    assert "account" in response.json()["detail"].lower()


def test_move_requires_provider_message_id(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda email: [_account_row(email)])
    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/classify-dryrun/move",
        json={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )
    assert response.status_code == 400
    assert "provider_message_id" in response.json()["detail"].lower()
