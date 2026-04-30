"""Tests for the approved inbox-move endpoint.

These tests pin the safety contract for ``POST /mail/inbox/move``:

* Auth: an unauthenticated session is rejected.
* Account scoping: a session can only move messages on an account it
  has actually linked.
* Body validation: ``provider_message_ids`` must be a non-empty array of
  strings, capped at ``INBOX_MOVE_MAX_BATCH``.
* Graph contract: the move is dispatched as ``POST
  /me/messages/{id}/move`` with ``destinationId`` resolved from the
  persisted folder inventory.
* Safety: ``forced_review`` rows (and rows whose recommendation is
  already ``10 - Review``) move only to ``10 - Review``, never to a
  business folder, even if the persisted recommendation says otherwise.
* Audit: every attempt persists a ``mailbox_move_action`` row via the
  persistence hook, with ``status`` reflecting succeeded / failed /
  rejected / skipped.
* Idempotency: a second move for a message that already has a
  ``succeeded`` row returns ``already_moved`` and does not re-call
  Graph.
* Missing dry-run row: the message is rejected without a Graph call.
* Missing destination folder in inventory: the message is skipped
  without a Graph call.
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


def _install_account_session(monkeypatch, email: str = "daniel@danielyoung.io"):
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda e: [_account_row(email)])

    async def fake_token(e):
        return "graph-token", {
            "account_id": "account-123",
            "email": e,
            "display_name": "Daniel Young",
        }

    monkeypatch.setattr(main, "_graph_access_token_for_email", fake_token)


def _install_dry_run_row(
    monkeypatch,
    *,
    recommended_folder: str = "20 - News",
    forced_review: bool = False,
    message_id: str = "msg-1",
):
    def fake_load(account_id, provider_message_id):
        if provider_message_id == message_id:
            return {
                "id": "dryrun-row-id",
                "provider_message_id": provider_message_id,
                "recommended_folder": recommended_folder,
                "forced_review": forced_review,
                "category": "newsletters_news",
                "confidence": 0.92,
                "confidence_band": "high",
                "current_folder": "inbox-folder-id",
            }
        return None

    monkeypatch.setattr(main, "_load_dry_run_row", fake_load)


def _install_no_existing_move(monkeypatch):
    monkeypatch.setattr(main, "_existing_succeeded_move", lambda account_id, msg_id: None)


def _install_inventory(monkeypatch, mapping: dict[str, str]):
    """``mapping`` maps canonical_name -> provider_folder_id."""

    def fake_resolver(account_id, recommended_folder):
        folder_id = mapping.get(recommended_folder)
        return folder_id, recommended_folder if folder_id else recommended_folder

    monkeypatch.setattr(main, "_resolve_target_folder_id", fake_resolver)


def test_move_requires_session(monkeypatch):
    monkeypatch.setattr(main, "settings", _local_settings())
    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/move",
        params={"account": "daniel@danielyoung.io"},
        json={"provider_message_ids": ["msg-1"]},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "No linked account session found."


def test_move_rejects_account_not_linked(monkeypatch):
    _install_account_session(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/move",
        params={"account": "stranger@example.com"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
        json={"provider_message_ids": ["msg-1"]},
    )
    assert response.status_code == 404
    assert "is linked" in response.json()["detail"].lower()


def test_move_rejects_empty_or_invalid_body(monkeypatch):
    _install_account_session(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/mail/inbox/move",
        params={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
        json={"provider_message_ids": []},
    )
    assert response.status_code == 400

    response = client.post(
        "/mail/inbox/move",
        params={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
        json={"provider_message_ids": "msg-1"},
    )
    assert response.status_code == 400

    response = client.post(
        "/mail/inbox/move",
        params={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
        json={"provider_message_ids": [""]},
    )
    assert response.status_code == 400


def test_move_caps_batch_size(monkeypatch):
    _install_account_session(monkeypatch)
    client = TestClient(main.app)
    too_many = [f"msg-{i}" for i in range(main.INBOX_MOVE_MAX_BATCH + 1)]
    response = client.post(
        "/mail/inbox/move",
        params={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
        json={"provider_message_ids": too_many},
    )
    assert response.status_code == 400


def test_move_calls_graph_move_with_resolved_destination(monkeypatch):
    """Happy path: persisted dry-run row + inventoried folder + Graph success."""
    _install_account_session(monkeypatch)
    _install_no_existing_move(monkeypatch)
    _install_dry_run_row(
        monkeypatch,
        recommended_folder="20 - News",
        forced_review=False,
        message_id="msg-1",
    )
    _install_inventory(monkeypatch, {"20 - News": "graph-news-id"})

    graph_calls: list[tuple[str, str, dict | None]] = []

    async def fake_graph_post(token, path, payload):
        graph_calls.append(("POST", path, payload))
        return {"id": "moved-msg-1"}

    async def fail_graph_get(*args, **kwargs):
        graph_calls.append(("GET", args[1] if len(args) > 1 else "?", None))
        raise AssertionError("/mail/inbox/move must not issue Graph GETs from the move path")

    monkeypatch.setattr(main, "_graph_post", fake_graph_post)
    monkeypatch.setattr(main, "_graph_get", fail_graph_get)

    persisted: list[dict] = []

    def fake_persist(**kwargs):
        persisted.append(kwargs)
        return "action-row-id"

    monkeypatch.setattr(main, "_persist_move_action", fake_persist)

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/move",
        params={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
        json={"provider_message_ids": ["msg-1"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["requested"] == 1
    assert payload["succeeded"] == 1
    assert payload["failed"] == 0
    assert payload["rejected"] == 0
    assert payload["skipped"] == 0
    assert payload["already_moved"] == 0
    assert payload["review_folder"] == "10 - Review"

    result = payload["results"][0]
    assert result["status"] == "succeeded"
    assert result["destination_folder_id"] == "graph-news-id"
    assert result["destination_folder_name"] == "20 - News"
    assert result["forced_review"] is False

    # Graph was hit exactly once with the move endpoint and destinationId.
    assert len(graph_calls) == 1
    method, path, body = graph_calls[0]
    assert method == "POST"
    assert path == "/me/messages/msg-1/move"
    assert body == {"destinationId": "graph-news-id"}

    # Audit row was written with status=succeeded and forced_review=False.
    assert len(persisted) == 1
    audit = persisted[0]
    assert audit["status"] == "succeeded"
    assert audit["forced_review"] is False
    assert audit["destination_folder_id"] == "graph-news-id"
    assert audit["destination_folder_name"] == "20 - News"
    assert audit["dry_run_classification_id"] == "dryrun-row-id"
    assert audit["requested_by_email"] == "daniel@danielyoung.io"
    assert audit["completed"] is True


def test_move_forced_review_rows_only_go_to_review_folder(monkeypatch):
    """Even if recommendation is a business folder, forced_review must route to 10 - Review."""
    _install_account_session(monkeypatch)
    _install_no_existing_move(monkeypatch)
    # The dry-run row says 20 - News but forced_review=True. The move
    # endpoint must override to 10 - Review.
    _install_dry_run_row(
        monkeypatch,
        recommended_folder="20 - News",
        forced_review=True,
        message_id="msg-risky",
    )
    _install_inventory(
        monkeypatch,
        {
            "10 - Review": "graph-review-id",
            "20 - News": "graph-news-id",
        },
    )

    graph_calls: list[dict] = []

    async def fake_graph_post(token, path, payload):
        graph_calls.append({"path": path, "payload": payload})
        return {"id": "moved"}

    monkeypatch.setattr(main, "_graph_post", fake_graph_post)
    monkeypatch.setattr(main, "_persist_move_action", lambda **kwargs: "row-id")

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/move",
        params={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
        json={"provider_message_ids": ["msg-risky"]},
    )

    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]
    assert result["status"] == "succeeded"
    assert result["forced_review"] is True
    # The destination is the review folder, not 20 - News.
    assert result["destination_folder_name"] == "10 - Review"
    assert result["destination_folder_id"] == "graph-review-id"
    # And the Graph call carried the review folder id.
    assert graph_calls == [
        {
            "path": "/me/messages/msg-risky/move",
            "payload": {"destinationId": "graph-review-id"},
        }
    ]


def test_move_rejects_messages_without_a_dry_run_row(monkeypatch):
    """A message id with no persisted dry-run row is rejected — no Graph call."""
    _install_account_session(monkeypatch)
    _install_no_existing_move(monkeypatch)
    monkeypatch.setattr(main, "_load_dry_run_row", lambda a, m: None)

    async def fail_graph_post(*args, **kwargs):
        raise AssertionError("must not call Graph for messages without a dry-run row")

    monkeypatch.setattr(main, "_graph_post", fail_graph_post)

    persisted: list[dict] = []
    monkeypatch.setattr(
        main,
        "_persist_move_action",
        lambda **kwargs: persisted.append(kwargs) or "row",
    )

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/move",
        params={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
        json={"provider_message_ids": ["msg-unknown"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["rejected"] == 1
    assert payload["succeeded"] == 0
    result = payload["results"][0]
    assert result["status"] == "rejected"
    assert result["error"] == "no_dry_run_row"

    # An audit row is still written to capture the rejection.
    assert len(persisted) == 1
    audit = persisted[0]
    assert audit["status"] == "rejected"
    assert audit["error"] == "no_dry_run_row"


def test_move_skips_when_destination_folder_not_inventoried(monkeypatch):
    """If the destination folder isn't in the inventory, skip and audit."""
    _install_account_session(monkeypatch)
    _install_no_existing_move(monkeypatch)
    _install_dry_run_row(
        monkeypatch,
        recommended_folder="20 - News",
        forced_review=False,
        message_id="msg-1",
    )
    _install_inventory(monkeypatch, {})

    async def fail_graph_post(*args, **kwargs):
        raise AssertionError("must not call Graph when destination folder is not inventoried")

    monkeypatch.setattr(main, "_graph_post", fail_graph_post)

    persisted: list[dict] = []
    monkeypatch.setattr(
        main,
        "_persist_move_action",
        lambda **kwargs: persisted.append(kwargs) or "row",
    )

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/move",
        params={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
        json={"provider_message_ids": ["msg-1"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["skipped"] == 1
    assert payload["succeeded"] == 0
    result = payload["results"][0]
    assert result["status"] == "skipped"
    assert result["error"] == "destination_folder_not_inventoried"
    assert result["destination_folder_name"] == "20 - News"

    assert len(persisted) == 1
    assert persisted[0]["status"] == "skipped"


def test_move_is_idempotent_when_existing_succeeded_row_present(monkeypatch):
    """A second move for a message with a succeeded row must not re-call Graph."""
    _install_account_session(monkeypatch)

    monkeypatch.setattr(
        main,
        "_existing_succeeded_move",
        lambda account_id, msg_id: {
            "id": "prior-action",
            "destination_folder_id": "graph-news-id",
            "destination_folder_name": "20 - News",
            "forced_review": False,
            "completed_at": "2026-04-29T00:00:00+00:00",
        },
    )

    async def fail_graph_post(*args, **kwargs):
        raise AssertionError("idempotent move must not call Graph when a succeeded row exists")

    monkeypatch.setattr(main, "_graph_post", fail_graph_post)

    def fail_load(account_id, message_id):
        raise AssertionError("idempotent path must not load dry-run row")

    def fail_persist(**kwargs):
        raise AssertionError("idempotent path must not write a new audit row")

    monkeypatch.setattr(main, "_load_dry_run_row", fail_load)
    monkeypatch.setattr(main, "_persist_move_action", fail_persist)

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/move",
        params={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
        json={"provider_message_ids": ["msg-1"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["already_moved"] == 1
    assert payload["succeeded"] == 0
    result = payload["results"][0]
    assert result["status"] == "already_moved"
    assert result["destination_folder_name"] == "20 - News"


def test_move_records_failed_audit_when_graph_fails(monkeypatch):
    """A Graph 4xx/5xx must persist a status=failed row and not raise."""
    from fastapi import HTTPException

    _install_account_session(monkeypatch)
    _install_no_existing_move(monkeypatch)
    _install_dry_run_row(
        monkeypatch,
        recommended_folder="20 - News",
        forced_review=False,
        message_id="msg-1",
    )
    _install_inventory(monkeypatch, {"20 - News": "graph-news-id"})

    async def fake_graph_post(token, path, payload):
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Microsoft Graph write request failed",
                "status_code": 403,
                "body": "InsufficientPermissions",
            },
        )

    monkeypatch.setattr(main, "_graph_post", fake_graph_post)

    persisted: list[dict] = []
    monkeypatch.setattr(
        main,
        "_persist_move_action",
        lambda **kwargs: persisted.append(kwargs) or "row",
    )

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/move",
        params={"account": "daniel@danielyoung.io"},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
        json={"provider_message_ids": ["msg-1"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["failed"] == 1
    assert payload["succeeded"] == 0
    result = payload["results"][0]
    assert result["status"] == "failed"
    assert result["error"] == "Microsoft Graph write request failed"

    assert len(persisted) == 1
    audit = persisted[0]
    assert audit["status"] == "failed"
    assert "Graph" in (audit["error"] or "")
    # The destination folder id was resolved before the call, so it is on
    # the audit row even though completed=False.
    assert audit["destination_folder_id"] == "graph-news-id"
    assert audit["completed"] is False


def test_activity_log_includes_move_events(monkeypatch):
    """/activity must surface mailbox_move_action rows under message_movement."""
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setattr(main, "_list_user_accounts", lambda e: [_account_row(e)])
    monkeypatch.setattr(main, "_load_folder_activity", lambda account_id, limit=25: [])
    monkeypatch.setattr(
        main,
        "_load_move_actions",
        lambda account_id, limit: [
            {
                "provider_message_id": "msg-1",
                "account_email": "daniel@danielyoung.io",
                "source_folder_id": "inbox-folder-id",
                "destination_folder_id": "graph-news-id",
                "destination_folder_name": "20 - News",
                "forced_review": False,
                "status": "succeeded",
                "error": None,
                "requested_by_email": "daniel@danielyoung.io",
                "requested_at": "2026-04-29T00:00:00+00:00",
                "completed_at": "2026-04-29T00:00:01+00:00",
            },
            {
                "provider_message_id": "msg-2",
                "account_email": "daniel@danielyoung.io",
                "source_folder_id": "inbox-folder-id",
                "destination_folder_id": "graph-review-id",
                "destination_folder_name": "10 - Review",
                "forced_review": True,
                "status": "succeeded",
                "error": None,
                "requested_by_email": "daniel@danielyoung.io",
                "requested_at": "2026-04-29T00:00:02+00:00",
                "completed_at": "2026-04-29T00:00:03+00:00",
            },
        ],
    )

    client = TestClient(main.app)
    response = client.get(
        "/activity",
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )
    assert response.status_code == 200
    payload = response.json()
    movement = payload["message_movement"]
    assert movement["available"] is True
    events = movement["events"]
    assert len(events) == 2
    # Sorted by occurred_at desc: msg-2 (forced) is newer.
    assert events[0]["move"]["provider_message_id"] == "msg-2"
    assert events[0]["move"]["forced_review"] is True
    assert events[0]["move"]["destination_folder_name"] == "10 - Review"
    assert events[0]["event_type"] == "message.move.succeeded"
    assert events[1]["move"]["provider_message_id"] == "msg-1"
    # No pending_instrumentation when movement is available.
    assert payload["pending_instrumentation"] == []


def test_automove_moves_only_high_confidence_allowed_category(monkeypatch):
    _install_account_session(monkeypatch)
    monkeypatch.setattr(
        main,
        "_automation_health_for_account",
        lambda row: {
            "state": "green",
            "label": "Automation ready",
            "automation_ready": True,
            "reasons": [],
        },
    )

    async def fake_list_messages(token, scan_limit):
        return [
            {
                "id": "msg-1",
                "subject": "CI run failed",
                "bodyPreview": "Repository build failed",
                "parentFolderId": "inbox-folder-id",
                "from": {"emailAddress": {"address": "notifications@github.com"}},
            }
        ]

    monkeypatch.setattr(main, "_list_inbox_messages_paginated", fake_list_messages)

    async def fake_classify(ci, provider_config=None):
        return classifier_module.ClassificationDecision(
            recommended_folder="90 - IT Reports",
            category="it_reports",
            confidence=0.96,
            confidence_band="high",
            reasons=["repository notification"],
            safety_flags=[],
            forced_review=False,
            provider_consulted=True,
            provider="azure_openai",
        )

    monkeypatch.setattr(classifier_module, "classify_with_provider", fake_classify)
    monkeypatch.setattr(main, "_persist_dry_run_classification", lambda **kwargs: None)
    monkeypatch.setattr(
        main,
        "_load_dry_run_row",
        lambda account_id, message_id: {
            "id": "dryrun-row-id",
            "current_folder": "inbox-folder-id",
        },
    )
    monkeypatch.setattr(main, "_existing_succeeded_move", lambda account_id, msg_id: None)
    _install_inventory(monkeypatch, {"90 - IT Reports": "it-folder-id"})

    graph_calls: list[tuple[str, str, dict]] = []

    async def fake_graph_post(token, path, payload):
        graph_calls.append((token, path, payload))
        return {"id": "moved-msg-1"}

    monkeypatch.setattr(main, "_graph_post", fake_graph_post)
    category_calls: list[tuple[str, str, dict]] = []

    async def fake_graph_patch(token, path, payload):
        category_calls.append((token, path, payload))
        return {}

    monkeypatch.setattr(main, "_graph_patch", fake_graph_patch)
    persisted: list[dict] = []
    monkeypatch.setattr(
        main,
        "_persist_move_action",
        lambda **kwargs: persisted.append(kwargs) or "action-row-id",
    )

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/automove",
        params={"account": "daniel@danielyoung.io", "limit": 1},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["automation"] is True
    assert payload["moved"] == 1
    assert payload["skipped"] == 0
    assert graph_calls == [
        ("graph-token", "/me/messages/msg-1/move", {"destinationId": "it-folder-id"})
    ]
    assert category_calls == [
        (
            "graph-token",
            "/me/messages/msg-1",
            {"categories": ["DYC - Automation Moved", "DYC - FYI"]},
        )
    ]
    assert persisted[0]["status"] == "succeeded"
    assert persisted[0]["destination_folder_name"] == "90 - IT Reports"


def test_automove_scans_deeper_than_move_limit(monkeypatch):
    _install_account_session(monkeypatch)
    monkeypatch.setattr(
        main,
        "_automation_health_for_account",
        lambda row: {
            "state": "green",
            "label": "Automation ready",
            "automation_ready": True,
            "reasons": [],
        },
    )

    async def fake_list_messages(token, scan_limit):
        assert scan_limit == 3
        return [
            {
                "id": "msg-review",
                "subject": "Contract terms",
                "bodyPreview": "Please review this agreement",
                "parentFolderId": "inbox-folder-id",
                "from": {"emailAddress": {"address": "legal@example.com"}},
            },
            {
                "id": "msg-move-1",
                "subject": "CI run failed",
                "bodyPreview": "Repository build failed",
                "parentFolderId": "inbox-folder-id",
                "from": {"emailAddress": {"address": "notifications@github.com"}},
            },
            {
                "id": "msg-move-2",
                "subject": "Another CI run failed",
                "bodyPreview": "Repository build failed",
                "parentFolderId": "inbox-folder-id",
                "from": {"emailAddress": {"address": "notifications@github.com"}},
            },
        ]

    monkeypatch.setattr(main, "_list_inbox_messages_paginated", fake_list_messages)

    async def fake_classify(ci, provider_config=None):
        if ci.subject == "Contract terms":
            return classifier_module.ClassificationDecision(
                recommended_folder="10 - Review",
                category="legal_contracts",
                confidence=0.99,
                confidence_band="high",
                reasons=["legal"],
                safety_flags=["legal_or_contractual"],
                forced_review=True,
                provider_consulted=True,
                provider="azure_openai",
            )
        return classifier_module.ClassificationDecision(
            recommended_folder="90 - IT Reports",
            category="it_reports",
            confidence=0.99,
            confidence_band="high",
            reasons=["repository notification"],
            safety_flags=[],
            forced_review=False,
            provider_consulted=True,
            provider="azure_openai",
        )

    monkeypatch.setattr(classifier_module, "classify_with_provider", fake_classify)
    monkeypatch.setattr(main, "_persist_dry_run_classification", lambda **kwargs: None)
    monkeypatch.setattr(
        main,
        "_load_dry_run_row",
        lambda account_id, message_id: {
            "id": f"dryrun-{message_id}",
            "current_folder": "inbox-folder-id",
        },
    )
    monkeypatch.setattr(main, "_existing_succeeded_move", lambda account_id, msg_id: None)
    _install_inventory(monkeypatch, {"90 - IT Reports": "it-folder-id"})

    graph_calls: list[tuple[str, str, dict]] = []

    async def fake_graph_post(token, path, payload):
        graph_calls.append((token, path, payload))
        return {"id": "moved-msg"}

    monkeypatch.setattr(main, "_graph_post", fake_graph_post)
    category_calls: list[tuple[str, str, dict]] = []

    async def fake_graph_patch(token, path, payload):
        category_calls.append((token, path, payload))
        return {}

    monkeypatch.setattr(main, "_graph_patch", fake_graph_patch)
    monkeypatch.setattr(main, "_persist_move_action", lambda **kwargs: "action-row-id")

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/automove",
        params={"account": "daniel@danielyoung.io", "limit": 3, "move_limit": 1},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["fetched"] == 3
    assert payload["moved"] == 1
    assert payload["skipped"] == 2
    assert graph_calls == [
        ("graph-token", "/me/messages/msg-move-1/move", {"destinationId": "it-folder-id"})
    ]
    assert category_calls == [
        (
            "graph-token",
            "/me/messages/msg-move-1",
            {"categories": ["DYC - Automation Moved", "DYC - FYI"]},
        ),
    ]
    assert payload["results"][2]["error"] == "automation_move_limit_reached"


def test_automove_skips_forced_review_without_graph_call(monkeypatch):
    _install_account_session(monkeypatch)
    monkeypatch.setattr(
        main,
        "_automation_health_for_account",
        lambda row: {
            "state": "green",
            "label": "Automation ready",
            "automation_ready": True,
            "reasons": [],
        },
    )

    async def fake_list_messages(token, scan_limit):
        return [
            {
                "id": "msg-1",
                "subject": "Contract terms",
                "bodyPreview": "Please review this agreement",
                "parentFolderId": "inbox-folder-id",
                "from": {"emailAddress": {"address": "legal@example.com"}},
            }
        ]

    monkeypatch.setattr(main, "_list_inbox_messages_paginated", fake_list_messages)

    async def fake_classify(ci, provider_config=None):
        return classifier_module.ClassificationDecision(
            recommended_folder="10 - Review",
            category="legal_contracts",
            confidence=0.99,
            confidence_band="high",
            reasons=["legal"],
            safety_flags=["legal_or_contractual"],
            forced_review=True,
            provider_consulted=True,
            provider="azure_openai",
        )

    monkeypatch.setattr(classifier_module, "classify_with_provider", fake_classify)
    monkeypatch.setattr(main, "_persist_dry_run_classification", lambda **kwargs: None)
    monkeypatch.setattr(
        main,
        "_load_dry_run_row",
        lambda account_id, message_id: {
            "id": "dryrun-row-id",
            "current_folder": "inbox-folder-id",
        },
    )

    async def fail_graph_post(*args, **kwargs):
        raise AssertionError("forced-review automation must not call Graph move")

    monkeypatch.setattr(main, "_graph_post", fail_graph_post)
    category_calls: list[tuple[str, str, dict]] = []

    async def fake_graph_patch(token, path, payload):
        category_calls.append((token, path, payload))
        return {}

    monkeypatch.setattr(main, "_graph_patch", fake_graph_patch)
    persisted: list[dict] = []
    monkeypatch.setattr(
        main,
        "_persist_move_action",
        lambda **kwargs: persisted.append(kwargs) or "action-row-id",
    )

    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/automove",
        params={"account": "daniel@danielyoung.io", "limit": 1},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["moved"] == 0
    assert payload["skipped"] == 1
    assert payload["results"][0]["error"] == "automation_forced_review"
    assert category_calls == []
    assert persisted[0]["status"] == "skipped"
    assert persisted[0]["error"] == "automation_forced_review"


def test_automove_rejects_unhealthy_account(monkeypatch):
    _install_account_session(monkeypatch)
    monkeypatch.setattr(
        main,
        "_automation_health_for_account",
        lambda row: {
            "state": "red",
            "label": "Folders missing",
            "automation_ready": False,
            "reasons": ["Run Bootstrap."],
        },
    )
    client = TestClient(main.app)
    response = client.post(
        "/mail/inbox/automove",
        params={"account": "daniel@danielyoung.io", "limit": 1},
        cookies={main.EMAIL_COOKIE: "daniel@danielyoung.io"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["automation_health"]["state"] == "red"


def test_scheduled_automation_requires_token(monkeypatch):
    monkeypatch.delenv("AUTOMATION_RUN_TOKEN", raising=False)
    client = TestClient(main.app)
    response = client.post("/automation/run")
    assert response.status_code == 503

    monkeypatch.setenv("AUTOMATION_RUN_TOKEN", "expected-token")
    response = client.post(
        "/automation/run",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_scheduled_automation_runs_yellow_and_skips_red(monkeypatch):
    monkeypatch.setenv("AUTOMATION_RUN_TOKEN", "expected-token")
    accounts = [
        _account_row("yellow@example.com"),
        _account_row("red@example.com"),
    ]
    monkeypatch.setattr(main, "_list_automation_accounts", lambda: accounts)

    def fake_health(row):
        if row["email"] == "red@example.com":
            return {
                "state": "red",
                "label": "Reconnect required",
                "automation_ready": False,
                "reasons": ["Reconnect."],
            }
        return {
            "state": "yellow",
            "label": "Folders incomplete",
            "automation_ready": True,
            "reasons": ["Bootstrap recommended."],
        }

    monkeypatch.setattr(main, "_automation_health_for_account", fake_health)

    async def fake_automove(requested_by_email, target, scan_limit, move_limit, min_confidence):
        assert requested_by_email == "automation@scheduler"
        assert target["email"] == "yellow@example.com"
        assert scan_limit == main.INBOX_AUTOMATION_DEFAULT_SCAN_LIMIT
        assert move_limit == main.INBOX_AUTOMATION_DEFAULT_MOVE_LIMIT
        return {
            "account": {"email": target["email"]},
            "moved": 2,
            "skipped": 1,
            "failed": 0,
        }

    monkeypatch.setattr(main, "_automove_for_account", fake_automove)

    client = TestClient(main.app)
    response = client.post(
        "/automation/run",
        headers={"Authorization": "Bearer expected-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scheduled"] is True
    assert payload["accounts_seen"] == 2
    assert payload["accounts_completed"] == 1
    assert payload["accounts_skipped"] == 1
    assert payload["moved"] == 2
