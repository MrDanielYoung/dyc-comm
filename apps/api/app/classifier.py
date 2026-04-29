"""AI classification decision contract for DYC Comm email triage.

This module defines the safe, dry-run classifier used by future workers
(and exposed by the dry-run ``/classify/recommend`` endpoint). It does not
send mail, does not move mail, does not delete mail. It returns a
recommendation only.

Design goals (see docs/ai-classifier-policy.md and architecture.md §9):

* The decision contract is stable: ``ClassificationDecision`` is the shape
  every caller and test depends on.
* The fallback folder is always ``10 - Review``. Whenever the classifier is
  uncertain, or the message hits an always-route-to-review category
  (legal/contractual ambiguity, sensitive customer-/patient-adjacent
  content, short emails without context, judgment-required tone), the
  recommendation is forced to ``10 - Review`` regardless of any model
  signal.
* Provider integration (Azure OpenAI / Azure AI) is scaffolding only.
  The deterministic path here does not call out to any provider; it is
  safe to run with no credentials. The provider config exists so future
  workers can swap in a real call without changing the contract.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

REVIEW_FOLDER = "10 - Review"
logger = logging.getLogger("dyc_comm.classifier")

# Allowed primary categories. Mirrors architecture.md §8 taxonomy.
ALLOWED_CATEGORIES: tuple[str, ...] = (
    "human_direct",
    "health_family",
    "finance_money",
    "meetings_scheduling",
    "access_auth",
    "service_updates",
    "it_reports",
    "newsletters_news",
    "marketing_promotions",
    "notifications_system",
    "legal_contracts",
    "unknown_ambiguous",
)

# Confidence band thresholds. Anything below MEDIUM_THRESHOLD is forced to
# Review. The numbers mirror architecture.md §10.
HIGH_THRESHOLD = 0.90
MEDIUM_THRESHOLD = 0.70

# Heuristic keyword sets used by the deterministic safety pass. These are
# intentionally conservative — false positives just route to 10 - Review,
# which is the safe outcome.
SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "patient",
    "patients",
    "phi",
    "protected health",
    "diagnosis",
    "medical record",
    "clinical",
    "biotronik",
    "implant",
    "pacemaker",
    "customer complaint",
    "complaint",
    "ssn",
    "social security",
    "passport number",
)

LEGAL_KEYWORDS: tuple[str, ...] = (
    "nda",
    "non-disclosure",
    "msa",
    "master services agreement",
    "contract",
    "agreement",
    "terms and conditions",
    "subpoena",
    "litigation",
    "settlement",
    "lawsuit",
    "counsel",
    "attorney",
    "legal hold",
)

# Phrases that suggest the newest message in a thread changes meaning of
# the prior thread — e.g. "actually never mind", "scratch that". These are
# soft signals; their presence forces routing to Review when the category
# is anything other than a clear deterministic match.
THREAD_FLIP_PHRASES: tuple[str, ...] = (
    "scratch that",
    "never mind",
    "nevermind",
    "actually, ignore",
    "disregard",
    "correction:",
    "ignore my last",
    "ignore previous",
    "update:",
)

JUDGMENT_PHRASES: tuple[str, ...] = (
    "as you know",
    "between us",
    "off the record",
    "politically",
    "tone-wise",
    "diplomatic",
    "obligation",
    "we are obligated",
    "you are obligated",
    "strictly confidential",
    "confidential",
)

# Minimum body length (in non-whitespace characters) before we trust any
# specific-folder routing. Below this we always route to Review.
SHORT_BODY_MIN_CHARS = 40


@dataclass(frozen=True)
class ClassificationInput:
    """Minimal, sanitized input to the classifier.

    Callers must pass already-redacted content. The classifier itself does
    not strip PII — that is the caller's responsibility (see data
    minimization notes in docs/ai-classifier-policy.md).
    """

    subject: str = ""
    body: str = ""
    sender: str = ""
    # When the input represents the newest message in a multi-message
    # thread, callers should set this so the classifier can apply the
    # thread-flip safety rule.
    is_thread_reply: bool = False
    # Optional caller-provided category hint from deterministic rules (e.g.
    # an allow-listed news sender). When set with a confidence >= 0.90 the
    # classifier may honor it; otherwise it is treated as advisory.
    rule_category: str | None = None
    rule_confidence: float = 0.0


@dataclass(frozen=True)
class ClassificationDecision:
    """Stable decision contract returned to callers.

    ``recommended_folder`` is the only routing signal callers should act
    on. It is always either a known DYC-managed folder name or
    ``10 - Review``. ``category`` is informational and may differ from the
    folder when the safety pass forces a review override (in that case the
    category remains the model's best guess but the folder is forced).
    """

    category: str
    recommended_folder: str
    confidence: float
    confidence_band: Literal["high", "medium", "low"]
    reasons: tuple[str, ...]
    safety_flags: tuple[str, ...]
    forced_review: bool
    # Whether a real AI provider was consulted. In the deterministic-only
    # path this is False; future workers that call Azure OpenAI will set
    # this to True so audit logs can distinguish.
    provider_consulted: bool = False
    provider: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "recommended_folder": self.recommended_folder,
            "confidence": self.confidence,
            "confidence_band": self.confidence_band,
            "reasons": list(self.reasons),
            "safety_flags": list(self.safety_flags),
            "forced_review": self.forced_review,
            "provider_consulted": self.provider_consulted,
            "provider": self.provider,
        }


@dataclass(frozen=True)
class AzureAIProviderConfig:
    """Provider configuration for Azure OpenAI / Azure AI Foundry.

    Loaded via :meth:`from_env`. Presence of these env vars is what
    ``/config-check`` reports; values are never exposed in responses.

    The classifier can consult Azure OpenAI / Azure AI when this object is
    fully configured. Missing or failing provider calls fall back to the
    deterministic path without breaking dry-run classification.
    """

    provider: Literal["azure_openai", "azure_ai", "none"]
    endpoint: str | None
    deployment: str | None
    api_version: str | None
    has_api_key: bool

    @classmethod
    def from_env(cls) -> AzureAIProviderConfig:
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("AZURE_AI_ENDPOINT")
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("AZURE_AI_DEPLOYMENT")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION")
        has_api_key = bool(os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_AI_API_KEY"))
        provider = _select_provider()
        return cls(
            provider=provider,
            endpoint=endpoint,
            deployment=deployment,
            api_version=api_version,
            has_api_key=has_api_key,
        )

    def is_configured(self) -> bool:
        return (
            self.provider != "none"
            and bool(self.endpoint)
            and bool(self.deployment)
            and self.has_api_key
        )

    def api_key(self) -> str | None:
        if self.provider == "azure_openai":
            return os.getenv("AZURE_OPENAI_API_KEY")
        if self.provider == "azure_ai":
            return os.getenv("AZURE_AI_API_KEY")
        return None


def _select_provider() -> Literal["azure_openai", "azure_ai", "none"]:
    if os.getenv("AZURE_OPENAI_ENDPOINT") and (
        os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_USE_AAD")
    ):
        return "azure_openai"
    if os.getenv("AZURE_AI_ENDPOINT"):
        return "azure_ai"
    return "none"


def _band(confidence: float) -> Literal["high", "medium", "low"]:
    if confidence >= HIGH_THRESHOLD:
        return "high"
    if confidence >= MEDIUM_THRESHOLD:
        return "medium"
    return "low"


def _contains_any(text: str, needles: tuple[str, ...]) -> list[str]:
    """Return needles found in ``text``.

    Single-token needles are matched on word boundaries so e.g. ``nda``
    does not falsely match inside ``recommendations``. Multi-token
    needles use plain substring search so phrases keep matching across
    surrounding punctuation.
    """
    lowered = text.lower()
    hits: list[str] = []
    for needle in needles:
        if " " in needle or "-" in needle:
            if needle in lowered:
                hits.append(needle)
            continue
        pattern = rf"\b{re.escape(needle)}\b"
        if re.search(pattern, lowered):
            hits.append(needle)
    return hits


_SHORT_TOKEN_RE = re.compile(r"\S+")


def _non_whitespace_len(text: str) -> int:
    return sum(len(t) for t in _SHORT_TOKEN_RE.findall(text or ""))


@dataclass
class _SafetyAssessment:
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def add(self, flag: str, reason: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)
        if reason not in self.reasons:
            self.reasons.append(reason)


@dataclass(frozen=True)
class _ModelSignal:
    category: str
    confidence: float
    reasons: tuple[str, ...]
    provider_consulted: bool
    provider: str | None


def _assess_safety(payload: ClassificationInput) -> _SafetyAssessment:
    """Pure deterministic safety pass.

    Each flag here independently forces routing to ``10 - Review``. We
    record both the flag (machine-readable) and a short human reason so
    the audit log can explain why a message was held for review.
    """
    assessment = _SafetyAssessment()
    combined = f"{payload.subject}\n{payload.body}"

    sensitive_hits = _contains_any(combined, SENSITIVE_KEYWORDS)
    if sensitive_hits:
        assessment.add(
            "sensitive_content",
            f"matched sensitive terms: {', '.join(sorted(sensitive_hits))}",
        )

    legal_hits = _contains_any(combined, LEGAL_KEYWORDS)
    if legal_hits:
        assessment.add(
            "legal_or_contractual",
            f"matched legal/contractual terms: {', '.join(sorted(legal_hits))}",
        )

    judgment_hits = _contains_any(combined, JUDGMENT_PHRASES)
    if judgment_hits:
        assessment.add(
            "judgment_required",
            "message uses tone/politics/obligation language",
        )

    if payload.is_thread_reply:
        flip_hits = _contains_any(combined, THREAD_FLIP_PHRASES)
        if flip_hits:
            assessment.add(
                "thread_meaning_changed",
                "newest message contains a correction/retraction phrase",
            )

    if _non_whitespace_len(payload.body) < SHORT_BODY_MIN_CHARS:
        assessment.add(
            "short_without_context",
            "body is too short to classify confidently",
        )

    return assessment


def _normalize_rule_category(value: str | None) -> str:
    if not value:
        return "unknown_ambiguous"
    if value in ALLOWED_CATEGORIES:
        return value
    return "unknown_ambiguous"


def _sanitize_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _decision_from_signals(
    payload: ClassificationInput,
    *,
    provider_config: AzureAIProviderConfig | None,
    model_signal: _ModelSignal | None = None,
) -> ClassificationDecision:
    safety = _assess_safety(payload)
    reasons: list[str] = list(safety.reasons)

    rule_category = _normalize_rule_category(payload.rule_category)
    rule_confidence = _sanitize_confidence(payload.rule_confidence)

    category = rule_category
    confidence = rule_confidence
    provider_consulted = False
    provider = provider_config.provider if provider_config else None

    if rule_category != "unknown_ambiguous" and rule_confidence > 0:
        reasons.append(
            f"deterministic rule suggested {rule_category} at confidence {rule_confidence:.2f}"
        )

    if model_signal and model_signal.provider_consulted:
        provider_consulted = True
        provider = model_signal.provider
        if rule_category == "unknown_ambiguous" or model_signal.confidence > rule_confidence:
            category = model_signal.category
            confidence = model_signal.confidence
        reasons.extend(model_signal.reasons)

    forced_review = False

    if safety.flags:
        forced_review = True
        reasons.append("safety override: forcing route to 10 - Review")

    if confidence < MEDIUM_THRESHOLD:
        forced_review = True
        if not safety.flags:
            reasons.append("confidence below medium threshold — defaulting to 10 - Review")

    if category == "legal_contracts" and "legal_or_contractual" in safety.flags:
        forced_review = True

    recommended_folder = REVIEW_FOLDER if forced_review else _category_to_folder(category)

    return ClassificationDecision(
        category=category,
        recommended_folder=recommended_folder,
        confidence=confidence,
        confidence_band=_band(confidence),
        reasons=tuple(dict.fromkeys(reasons)),
        safety_flags=tuple(safety.flags),
        forced_review=forced_review,
        provider_consulted=provider_consulted,
        provider=provider,
    )


def classify(
    payload: ClassificationInput,
    *,
    provider_config: AzureAIProviderConfig | None = None,
) -> ClassificationDecision:
    """Return a safe routing recommendation for one message.

    This is dry-run only. The caller is expected to surface the decision
    to a human; nothing in this function moves, deletes, or sends mail.

    The function is deterministic. If a real Azure OpenAI / Azure AI
    provider is configured, future revisions may consult it, but only to
    *raise* confidence on already-allowed categories — never to bypass the
    forced-review rules below.
    """
    return _decision_from_signals(payload, provider_config=provider_config)


_CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "category": {
            "type": "string",
            "enum": list(ALLOWED_CATEGORIES),
            "description": "Best category for the email.",
        },
        "confidence": {
            "type": "number",
            "description": "Classifier confidence from 0.0 to 1.0.",
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short operator-readable reasons for the classification.",
        },
    },
    "required": ["category", "confidence", "reasons"],
}


def _provider_url(config: AzureAIProviderConfig) -> str:
    endpoint = (config.endpoint or "").rstrip("/")
    deployment = config.deployment or ""
    api_version = config.api_version or "2024-08-01-preview"
    return f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"


def _provider_messages(payload: ClassificationInput) -> list[dict[str, str]]:
    categories = ", ".join(ALLOWED_CATEGORIES)
    return [
        {
            "role": "system",
            "content": (
                "Classify one email for a private Microsoft 365 mailbox. "
                "Return only the strict JSON schema. Do not recommend actions. "
                f"Allowed categories: {categories}. Prefer unknown_ambiguous "
                "when the message needs human judgment."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "sender": payload.sender,
                    "subject": payload.subject,
                    "body_preview": payload.body,
                    "is_thread_reply": payload.is_thread_reply,
                    "rule_category_hint": payload.rule_category,
                    "rule_confidence_hint": payload.rule_confidence,
                },
                ensure_ascii=False,
            ),
        },
    ]


async def _call_azure_classifier(
    payload: ClassificationInput,
    config: AzureAIProviderConfig,
) -> _ModelSignal | None:
    api_key = config.api_key()
    if not config.is_configured() or not api_key:
        return None

    request_payload = {
        "messages": _provider_messages(payload),
        "temperature": 0,
        "max_completion_tokens": 400,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "email_classification",
                "strict": True,
                "schema": _CLASSIFICATION_SCHEMA,
            },
        },
    }
    headers = {"api-key": api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                _provider_url(config),
                headers=headers,
                json=request_payload,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception as exc:
        logger.warning("classifier.provider_failed provider=%s error=%s", config.provider, exc)
        return None

    category = _normalize_rule_category(parsed.get("category"))
    confidence = _sanitize_confidence(parsed.get("confidence"))
    raw_reasons = parsed.get("reasons")
    reasons = (
        tuple(str(item)[:240] for item in raw_reasons if item)
        if isinstance(raw_reasons, list)
        else ()
    )

    return _ModelSignal(
        category=category,
        confidence=confidence,
        reasons=reasons or (f"{config.provider} classified message as {category}",),
        provider_consulted=True,
        provider=config.provider,
    )


async def classify_with_provider(
    payload: ClassificationInput,
    *,
    provider_config: AzureAIProviderConfig | None = None,
) -> ClassificationDecision:
    """Classify with Azure OpenAI/Azure AI when configured.

    The provider only supplies category/confidence/reasons. The local
    deterministic safety pass still decides whether to force ``10 - Review``.
    """
    if not provider_config or not provider_config.is_configured():
        return classify(payload, provider_config=provider_config)

    model_signal = await _call_azure_classifier(payload, provider_config)
    return _decision_from_signals(
        payload,
        provider_config=provider_config,
        model_signal=model_signal,
    )


# Mapping from category to the DYC-managed folder name. Categories not
# listed here intentionally fall back to 10 - Review.
_CATEGORY_FOLDER_MAP: dict[str, str] = {
    "newsletters_news": "20 - News",
    "notifications_system": "40 - Notifications",
    "marketing_promotions": "50 - Marketing",
    "legal_contracts": "70 - Contracts",
    "it_reports": "90 - IT Reports",
}


def _category_to_folder(category: str) -> str:
    return _CATEGORY_FOLDER_MAP.get(category, REVIEW_FOLDER)
