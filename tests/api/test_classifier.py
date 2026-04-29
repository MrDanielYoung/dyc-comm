"""Tests for the dry-run AI classification decision contract.

These tests pin the behaviour the rest of the system (and any future
worker that swaps in a real Azure OpenAI / Azure AI call) is allowed to
rely on. The contract:

* Whenever the classifier is uncertain, sensitive, legal/contractual
  ambiguous, judgment-required, too short, or a thread-flip, the
  recommended folder is ``10 - Review``.
* The decision dataclass has a stable shape — ``to_dict()`` keys must not
  change without bumping the contract version.
* The classifier never recommends a destructive action.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import classifier as classifier_module
from apps.api.app import main
from apps.api.app.classifier import (
    REVIEW_FOLDER,
    AzureAIProviderConfig,
    ClassificationInput,
    classify,
)
from apps.api.app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Pure module tests
# ---------------------------------------------------------------------------


def test_decision_to_dict_has_stable_shape():
    decision = classify(
        ClassificationInput(
            subject="Quarterly newsletter",
            body=(
                "This is the latest issue of our weekly newsletter, with "
                "stories from across the industry and updates from the team."
            ),
            sender="news@example.com",
            rule_category="newsletters_news",
            rule_confidence=0.95,
        )
    )
    payload = decision.to_dict()
    assert set(payload.keys()) == {
        "category",
        "recommended_folder",
        "confidence",
        "confidence_band",
        "reasons",
        "safety_flags",
        "forced_review",
        "provider_consulted",
        "provider",
    }


def test_high_confidence_newsletter_routes_to_news():
    decision = classify(
        ClassificationInput(
            subject="Weekly News Roundup",
            body=(
                "Top stories this week: industry updates, market commentary, "
                "and reading recommendations from the editorial team."
            ),
            sender="newsletter@example.com",
            rule_category="newsletters_news",
            rule_confidence=0.95,
        )
    )
    assert decision.recommended_folder == "20 - News"
    assert decision.forced_review is False
    assert decision.confidence_band == "high"


def test_high_confidence_it_report_routes_to_it_reports():
    decision = classify(
        ClassificationInput(
            subject="[MrDanielYoung/dyc-comm] PR run failed: CI",
            body=(
                "The repository workflow run failed for the latest pull "
                "request. Review the CI logs and deployment status."
            ),
            sender="notifications@github.com",
            rule_category="it_reports",
            rule_confidence=0.95,
        )
    )
    assert decision.recommended_folder == "90 - IT Reports"
    assert decision.forced_review is False
    assert decision.confidence_band == "high"


def test_low_confidence_forces_review():
    decision = classify(
        ClassificationInput(
            subject="hey",
            body="quick question when you get a sec, thanks!!",
            rule_category="human_direct",
            rule_confidence=0.4,
        )
    )
    assert decision.recommended_folder == REVIEW_FOLDER
    assert decision.forced_review is True
    assert decision.confidence_band == "low"


def test_no_rule_signal_routes_to_review():
    decision = classify(
        ClassificationInput(
            subject="Some subject",
            body=(
                "Body text that is long enough to clear the short-context "
                "threshold but with no deterministic category signal at all."
            ),
        )
    )
    assert decision.recommended_folder == REVIEW_FOLDER
    assert decision.forced_review is True


def test_sensitive_keyword_forces_review_even_at_high_confidence():
    decision = classify(
        ClassificationInput(
            subject="Patient referral notes from cardiology team",
            body=(
                "Attached are the patient referral notes and pacemaker "
                "implant follow-up details from the cardiology clinic."
            ),
            rule_category="newsletters_news",
            rule_confidence=0.99,
        )
    )
    assert decision.recommended_folder == REVIEW_FOLDER
    assert "sensitive_content" in decision.safety_flags
    assert decision.forced_review is True


def test_biotronik_term_is_treated_as_sensitive():
    decision = classify(
        ClassificationInput(
            subject="BIOTRONIK device update",
            body=(
                "Following up on the BIOTRONIK pacemaker integration "
                "discussion — please review the attached documentation."
            ),
            rule_category="service_updates",
            rule_confidence=0.95,
        )
    )
    assert decision.recommended_folder == REVIEW_FOLDER
    assert "sensitive_content" in decision.safety_flags


def test_legal_keyword_forces_review_when_legal_category_uncertain():
    decision = classify(
        ClassificationInput(
            subject="Re: NDA terms",
            body=(
                "Could you take a look at the NDA and let us know your "
                "thoughts on the indemnification clause before counsel weighs in?"
            ),
            rule_category="legal_contracts",
            rule_confidence=0.95,
        )
    )
    # legal_contracts may auto-route to 70 - Contracts only when the legal
    # safety flag is NOT triggered. Here it is, so this must go to Review.
    assert decision.recommended_folder == REVIEW_FOLDER
    assert "legal_or_contractual" in decision.safety_flags


def test_short_body_forces_review():
    decision = classify(
        ClassificationInput(
            subject="ok",
            body="thx",
            rule_category="human_direct",
            rule_confidence=0.95,
        )
    )
    assert decision.recommended_folder == REVIEW_FOLDER
    assert "short_without_context" in decision.safety_flags


def test_thread_flip_phrase_forces_review():
    decision = classify(
        ClassificationInput(
            subject="Re: project status",
            body=(
                "Actually, ignore my last message — the schedule has changed "
                "and we will need to revisit the next steps in detail."
            ),
            is_thread_reply=True,
            rule_category="human_direct",
            rule_confidence=0.95,
        )
    )
    assert decision.recommended_folder == REVIEW_FOLDER
    assert "thread_meaning_changed" in decision.safety_flags


def test_judgment_required_phrase_forces_review():
    decision = classify(
        ClassificationInput(
            subject="Confidential — internal only",
            body=(
                "Strictly confidential: please keep this between us until we "
                "have agreed on the right diplomatic framing for the response."
            ),
            rule_category="human_direct",
            rule_confidence=0.95,
        )
    )
    assert decision.recommended_folder == REVIEW_FOLDER
    assert "judgment_required" in decision.safety_flags


def test_unknown_rule_category_is_normalized():
    decision = classify(
        ClassificationInput(
            subject="Some subject line here",
            body=(
                "A reasonably long body that should be enough to clear the "
                "short-context threshold for the deterministic safety pass."
            ),
            rule_category="not_a_real_category",
            rule_confidence=0.99,
        )
    )
    # Unknown rule categories collapse to unknown_ambiguous and force review.
    assert decision.category == "unknown_ambiguous"
    assert decision.recommended_folder == REVIEW_FOLDER


def test_classifier_never_consults_provider_in_deterministic_path():
    cfg = AzureAIProviderConfig(
        provider="azure_openai",
        endpoint="https://example.openai.azure.com",
        deployment="gpt-4o-mini",
        api_version="2024-08-01-preview",
        has_api_key=True,
    )
    decision = classify(
        ClassificationInput(
            subject="Hello",
            body="A long enough message body to clear the short-context check.",
            rule_category="human_direct",
            rule_confidence=0.95,
        ),
        provider_config=cfg,
    )
    assert decision.provider_consulted is False
    assert decision.provider == "azure_openai"


# ---------------------------------------------------------------------------
# Endpoint contract tests
# ---------------------------------------------------------------------------


def _local_settings() -> main.Settings:
    return main.Settings(
        app_env="local",
        web_app_url="http://localhost:3000",
        api_base_url="http://localhost:8000",
        allowed_origins=["http://localhost:3000"],
        key_vault_refs_enabled=False,
        legacy_rule_folder_names=("Wolt",),
    )


def test_endpoint_returns_dry_run_envelope():
    response = client.post(
        "/classify/recommend",
        json={
            "subject": "Hello team",
            "body": (
                "Just checking in on the planning thread, would love your "
                "input on next quarter's roadmap when you have a minute."
            ),
            "sender": "alice@example.com",
            "rule_category": "human_direct",
            "rule_confidence": 0.85,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["review_folder"] == REVIEW_FOLDER
    assert payload["policy_version"] == "v1.0"
    assert "recommendation" in payload
    rec = payload["recommendation"]
    for key in (
        "category",
        "recommended_folder",
        "confidence",
        "confidence_band",
        "reasons",
        "safety_flags",
        "forced_review",
        "provider_consulted",
        "provider",
    ):
        assert key in rec, key


def test_endpoint_forces_review_for_sensitive_content():
    response = client.post(
        "/classify/recommend",
        json={
            "subject": "BIOTRONIK patient case follow-up",
            "body": (
                "Sharing the patient implant follow-up file from this "
                "morning's clinic for your records and review."
            ),
            "rule_category": "newsletters_news",
            "rule_confidence": 0.99,
        },
    )
    assert response.status_code == 200
    rec = response.json()["recommendation"]
    assert rec["recommended_folder"] == REVIEW_FOLDER
    assert rec["forced_review"] is True
    assert "sensitive_content" in rec["safety_flags"]


def test_endpoint_forces_review_for_low_confidence():
    response = client.post(
        "/classify/recommend",
        json={
            "subject": "Re: thoughts?",
            "body": "Can you let me know? Thanks again, talk soon.",
            "rule_category": "human_direct",
            "rule_confidence": 0.3,
        },
    )
    assert response.status_code == 200
    rec = response.json()["recommendation"]
    assert rec["recommended_folder"] == REVIEW_FOLDER
    assert rec["forced_review"] is True


def test_endpoint_rejects_invalid_rule_confidence():
    response = client.post(
        "/classify/recommend",
        json={
            "subject": "Hello",
            "body": "A long enough body to clear the short-context check.",
            "rule_confidence": "not-a-number",
        },
    )
    assert response.status_code == 400


def test_endpoint_handles_empty_body_safely():
    response = client.post("/classify/recommend", json={})
    assert response.status_code == 200
    rec = response.json()["recommendation"]
    # Empty inputs => no rule signal + short body => forced review.
    assert rec["recommended_folder"] == REVIEW_FOLDER
    assert rec["forced_review"] is True


# ---------------------------------------------------------------------------
# /config-check Azure OpenAI scaffolding
# ---------------------------------------------------------------------------


AZURE_AI_VARS = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_API_KEY",
    "AZURE_AI_ENDPOINT",
    "AZURE_AI_DEPLOYMENT",
    "AZURE_AI_API_KEY",
)


def test_config_check_lists_azure_ai_vars(monkeypatch):
    for var in AZURE_AI_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(main, "settings", _local_settings())

    response = client.get("/config-check")
    assert response.status_code == 200
    payload = response.json()
    variables = payload["variables"]
    for name in AZURE_AI_VARS:
        assert name in variables, name
        assert variables[name]["present"] is False
    # API keys must be marked secret.
    assert variables["AZURE_OPENAI_API_KEY"]["is_secret"] is True
    assert variables["AZURE_AI_API_KEY"]["is_secret"] is True
    # Non-secret config knobs must be marked non-secret.
    assert variables["AZURE_OPENAI_ENDPOINT"]["is_secret"] is False
    assert variables["AZURE_OPENAI_DEPLOYMENT"]["is_secret"] is False


def test_config_check_reports_azure_provider_block(monkeypatch):
    for var in AZURE_AI_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(main, "settings", _local_settings())
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "super-secret-key-do-not-leak")

    response = client.get("/config-check")
    assert response.status_code == 200
    payload = response.json()
    ai_provider = payload["ai_provider"]
    assert ai_provider["selected"] == "azure_openai"
    assert ai_provider["configured"] is True
    assert ai_provider["endpoint_present"] is True
    assert ai_provider["deployment_present"] is True
    assert ai_provider["api_key_present"] is True

    # No values must leak into the response.
    body = response.text
    for sensitive in (
        "super-secret-key-do-not-leak",
        "https://example.openai.azure.com",
        "gpt-4o-mini",
    ):
        assert sensitive not in body, f"value leaked into /config-check: {sensitive!r}"


def test_config_check_unset_azure_ai_does_not_break_required_present(monkeypatch):
    """Azure AI vars are optional — their absence must not flip
    ``all_required_present`` to False when the core Microsoft auth vars
    and DATABASE_URL are present."""
    for var in AZURE_AI_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_ID", "client-id")
    monkeypatch.setenv("MICROSOFT_ENTRA_TENANT_ID", "tenant-id")
    monkeypatch.setenv("MICROSOFT_ENTRA_CLIENT_SECRET", "secret")
    monkeypatch.setenv(
        "MICROSOFT_ENTRA_REDIRECT_URI",
        "http://localhost:8000/auth/microsoft/callback",
    )
    monkeypatch.setattr(main, "settings", _local_settings())

    response = client.get("/config-check")
    assert response.status_code == 200
    payload = response.json()
    assert payload["all_required_present"] is True
    assert payload["ai_provider"]["selected"] == "none"
    assert payload["ai_provider"]["configured"] is False


def test_provider_config_from_env_selects_azure_ai_fallback(monkeypatch):
    for var in AZURE_AI_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AZURE_AI_ENDPOINT", "https://example.ai.azure.com")
    monkeypatch.setenv("AZURE_AI_DEPLOYMENT", "phi-4")
    monkeypatch.setenv("AZURE_AI_API_KEY", "key")
    cfg = classifier_module.AzureAIProviderConfig.from_env()
    assert cfg.provider == "azure_ai"
    assert cfg.is_configured() is True
