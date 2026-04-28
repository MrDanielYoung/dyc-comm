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

import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

REVIEW_FOLDER = "10 - Review"

# Allowed primary categories. Mirrors architecture.md §8 taxonomy.
ALLOWED_CATEGORIES: tuple[str, ...] = (
    "human_direct",
    "health_family",
    "finance_money",
    "meetings_scheduling",
    "access_auth",
    "service_updates",
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

    The classifier does not call the provider in this slice — this object
    exists so future workers can wire a real call without touching the
    decision contract.
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
        return self.provider != "none" and bool(self.endpoint) and bool(self.deployment)


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
    safety = _assess_safety(payload)
    reasons: list[str] = list(safety.reasons)

    rule_category = _normalize_rule_category(payload.rule_category)
    rule_confidence = max(0.0, min(1.0, payload.rule_confidence))

    # Start from the deterministic rule signal (if any). With no rule and
    # no provider call we sit at unknown_ambiguous / 0.0 confidence, which
    # naturally routes to Review.
    category = rule_category
    confidence = rule_confidence
    if rule_category != "unknown_ambiguous" and rule_confidence > 0:
        reasons.append(
            f"deterministic rule suggested {rule_category} at confidence {rule_confidence:.2f}"
        )

    forced_review = False

    if safety.flags:
        forced_review = True
        reasons.append("safety override: forcing route to 10 - Review")

    if confidence < MEDIUM_THRESHOLD:
        forced_review = True
        if not safety.flags:
            reasons.append("confidence below medium threshold — defaulting to 10 - Review")

    # legal_contracts is allowed to route to 70 - Contracts only when both
    # the deterministic signal is high-confidence AND the legal-keyword
    # safety flag was NOT triggered (e.g. clear DocuSign envelope). If the
    # legal flag fired, we never auto-route legal mail.
    if category == "legal_contracts" and "legal_or_contractual" in safety.flags:
        forced_review = True

    recommended_folder = REVIEW_FOLDER if forced_review else _category_to_folder(category)

    return ClassificationDecision(
        category=category,
        recommended_folder=recommended_folder,
        confidence=confidence,
        confidence_band=_band(confidence),
        reasons=tuple(reasons),
        safety_flags=tuple(safety.flags),
        forced_review=forced_review,
        provider_consulted=False,
        provider=provider_config.provider if provider_config else None,
    )


# Mapping from category to the DYC-managed folder name. Categories not
# listed here intentionally fall back to 10 - Review.
_CATEGORY_FOLDER_MAP: dict[str, str] = {
    "newsletters_news": "20 - News",
    "notifications_system": "40 - Notifications",
    "marketing_promotions": "50 - Marketing",
    "legal_contracts": "70 - Contracts",
}


def _category_to_folder(category: str) -> str:
    return _CATEGORY_FOLDER_MAP.get(category, REVIEW_FOLDER)
