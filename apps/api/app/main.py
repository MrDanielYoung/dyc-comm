import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import Cookie, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from . import classifier as classifier_module

logger = logging.getLogger("dyc_comm.auth")

AUTH_COOKIE = "dyc_auth_state"
PKCE_COOKIE = "dyc_auth_pkce"
AUTH_TENANT_COOKIE = "dyc_auth_tenant"
EMAIL_COOKIE = "dyc_account_email"
NAME_COOKIE = "dyc_account_name"
MICROSOFT_SCOPE = (
    "openid profile email offline_access User.Read Mail.Read Mail.ReadWrite "
    "MailboxSettings.ReadWrite"
)
MICROSOFT_PROVIDER = "microsoft_365"
DEFAULT_LEGACY_RULE_FOLDER_NAMES = ("Wolt", "Amazon", "Komote", "Cycle Touring")
SYSTEM_FOLDER_NAMES = (
    "Inbox",
    "Drafts",
    "Sent Items",
    "Deleted Items",
    "Archive",
    "Junk Email",
    "Outbox",
)
DEFAULT_MVP_FOLDER_SPECS = (
    {"name": "10 - Review", "aliases": ("Review",)},
    {"name": "20 - News", "aliases": ("News",)},
    {"name": "30 - Money", "aliases": ("Money",)},
    {"name": "40 - Notifications", "aliases": ("Notifications",)},
    {"name": "50 - Marketing", "aliases": ("Marketing",)},
    {"name": "54 - LinkedIn", "aliases": ("LinkedIn", "30 - LinkedIn")},
    {"name": "56 - Pipedrive", "aliases": ("Pipedrive",)},
    {"name": "60 - Notes", "aliases": ("Notes",)},
    {"name": "70 - Contracts", "aliases": ("Contracts",)},
    {"name": "80 - Travel", "aliases": ("Travel",)},
    {"name": "90 - IT Reports", "aliases": ("IT Reports",)},
)
DEFAULT_OUTLOOK_CATEGORY_SPECS = (
    {"displayName": "< Today >", "color": "preset0"},
    {"displayName": "< This Week >", "color": "preset1"},
    {"displayName": "< Reply >", "color": "preset7"},
    {"displayName": "< Waiting >", "color": "preset3"},
    {"displayName": "< Read Later >", "color": "preset10"},
    {"displayName": "< FYI >", "color": "preset12"},
    {"displayName": "< Money >", "color": "preset4"},
    {"displayName": "< Legal >", "color": "preset9"},
    {"displayName": "< Customer >", "color": "preset8"},
    {"displayName": "< Travel >", "color": "preset5"},
    {"displayName": "< Review >", "color": "preset15"},
    {"displayName": "< Pay This >", "color": "preset2"},
)

_URGENT_PHRASES: tuple[str, ...] = (
    "urgent",
    "asap",
    "immediately",
    "right away",
    "time sensitive",
    "as soon as possible",
    "need to hear from you by",
    "respond by",
    "reply by",
    "get back to me by",
    "no later than",
    "deadline is",
    "need your response",
    "need a response",
    "due today",
    "by end of day",
    "by eod",
    "by cob",
    "reminder",
    "following up",
    "just checking in",
    "circling back",
    "haven't heard back",
    "wanted to follow up",
    "second request",
    "final notice",
    "last chance",
)

_INVOICE_PHRASES: tuple[str, ...] = (
    "invoice",
    "payment due",
    "amount due",
    "balance due",
    "please remit",
    "remittance",
    "payment requested",
    "attached invoice",
    "bill for services",
    "statement of account",
    "due on receipt",
)

_DB_BOOTSTRAPPED = False

# Runtime env-var contract reported by /config-check. Mirrors the variables
# documented in docs/azure-runtime-config.md and applied by
# infra/azure/apply-api-settings.sh. Values are never returned — only presence
# and whether a variable carries secret material.
_RUNTIME_VARIABLES: tuple[tuple[str, bool], ...] = (
    ("DATABASE_URL", True),
    ("MICROSOFT_ENTRA_CLIENT_ID", False),
    ("MICROSOFT_ENTRA_TENANT_ID", False),
    ("MICROSOFT_ENTRA_CLIENT_SECRET", True),
    ("MICROSOFT_ENTRA_REDIRECT_URI", False),
    ("KEY_VAULT_REFS_ENABLED", False),
    # Allow-list controls for the OAuth callback. Both are non-secret;
    # /config-check reports presence only (never values).
    ("ALLOWED_MICROSOFT_TENANT_IDS", False),
    ("ALLOWED_ACCOUNT_EMAILS", False),
    # Azure OpenAI / Azure AI provider scaffolding for the dry-run AI
    # classifier. All optional: the deterministic classifier runs without
    # any of these set. /config-check reports presence only.
    ("AZURE_OPENAI_ENDPOINT", False),
    ("AZURE_OPENAI_DEPLOYMENT", False),
    ("AZURE_OPENAI_API_VERSION", False),
    ("AZURE_OPENAI_API_KEY", True),
    ("AZURE_AI_ENDPOINT", False),
    ("AZURE_AI_DEPLOYMENT", False),
    ("AZURE_AI_API_KEY", True),
    ("AUTOMATION_RUN_TOKEN", True),
    # Azure Communication Services — SMS urgent alerts.
    ("ACS_CONNECTION_STRING", True),
    ("ACS_FROM_NUMBER", False),
    ("ALERT_PHONE_NUMBER", False),
)

# Env vars that are optional / advisory. Their absence must NOT cause
# /config-check to flag the runtime as not-ready. Mirrored in the alerts
# computation at the bottom of this file.
_OPTIONAL_RUNTIME_VARIABLES: frozenset[str] = frozenset(
    {
        "KEY_VAULT_REFS_ENABLED",
        "ALLOWED_MICROSOFT_TENANT_IDS",
        "ALLOWED_ACCOUNT_EMAILS",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_API_KEY",
        "AZURE_AI_ENDPOINT",
        "AZURE_AI_DEPLOYMENT",
        "AZURE_AI_API_KEY",
        "AUTOMATION_RUN_TOKEN",
        "ACS_CONNECTION_STRING",
        "ACS_FROM_NUMBER",
        "ALERT_PHONE_NUMBER",
    }
)


def _bool_env(name: str) -> bool:
    value = os.getenv(name, "")
    return value.lower() in {"1", "true", "yes", "on"}


def _split_csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    if not raw:
        return []
    return [value.strip() for value in raw.split(",") if value.strip()]


def _normalize_id(value: str | None) -> str:
    return (value or "").strip().lower()


def _allowed_tenant_ids() -> set[str]:
    """Tenants permitted to complete the OAuth callback.

    Default-deny posture: when ``ALLOWED_MICROSOFT_TENANT_IDS`` is not set
    we fall back to the single ``MICROSOFT_ENTRA_TENANT_ID`` configured for
    the app. External tenants must be opted in explicitly via the env var.
    Returning an empty set causes every callback to be rejected, which is
    the safe failure mode.
    """
    explicit = {_normalize_id(v) for v in _split_csv_env("ALLOWED_MICROSOFT_TENANT_IDS")}
    if explicit:
        return {v for v in explicit if v}
    home = _normalize_id(os.getenv("MICROSOFT_ENTRA_TENANT_ID"))
    return {home} if home else set()


def _allowed_account_emails() -> set[str]:
    """Email allow-list. Empty set means no per-email allow-list is enforced.

    When set, only the listed addresses (matched case-insensitively against
    Graph ``mail`` / ``userPrincipalName`` / ID token ``preferred_username``)
    may complete sign-in, even if their tenant id is allow-listed.
    """
    return {_normalize_id(v) for v in _split_csv_env("ALLOWED_ACCOUNT_EMAILS") if v}


def _visible_account_emails_for_session(user_email: str) -> list[str]:
    """Return allow-listed mailbox emails visible to this signed-in account.

    ``dyc-comm`` is currently a private, single-user console. Daniel may sign
    in through any allow-listed Microsoft mailbox, but the portal should still
    show the full connected mailbox set. Without this, connecting DHW changes
    the session email to DHW and hides the DYC mailbox.
    """
    allowed = _allowed_account_emails()
    normalized_user = _normalize_id(user_email)
    if not allowed or normalized_user not in allowed:
        return []
    return sorted(allowed)


def _decode_id_token_claims(id_token: str | None) -> dict[str, Any]:
    """Decode JWT payload without signature verification.

    The ID token here is consumed only to read authorization attributes
    (``tid``, ``preferred_username``) AFTER a successful exchange against
    Microsoft's token endpoint, which already authenticated the client
    over TLS using the client secret. We do not use it as proof of
    authentication. If the payload cannot be parsed we return ``{}`` and
    the caller falls back to Graph-derived identity, which on its own is
    not sufficient to assert tenant — callers must therefore reject the
    sign-in when ``tid`` cannot be established.
    """
    if not id_token:
        return {}
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(decoded)
        if isinstance(claims, dict):
            return claims
    except (ValueError, json.JSONDecodeError):
        return {}
    return {}


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise HTTPException(status_code=500, detail=f"Missing required env var: {name}")
    return value


def _database_url() -> str | None:
    return os.getenv("DATABASE_URL")


def _fold_name(name: str) -> str:
    return name.strip().casefold()


@dataclass(frozen=True)
class Settings:
    app_env: str
    web_app_url: str
    api_base_url: str
    allowed_origins: list[str]
    key_vault_refs_enabled: bool
    legacy_rule_folder_names: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        web_app_url = os.getenv("WEB_APP_URL", "https://comm.danielyoung.io")
        api_base_url = os.getenv("API_BASE_URL", "https://api.comm.danielyoung.io")
        allowed_origins = _split_csv_env("ALLOWED_ORIGINS")
        if not allowed_origins:
            allowed_origins = [web_app_url]
        legacy_rule_folder_names = tuple(_split_csv_env("LEGACY_RULE_FOLDER_NAMES"))
        if not legacy_rule_folder_names:
            legacy_rule_folder_names = DEFAULT_LEGACY_RULE_FOLDER_NAMES
        return cls(
            app_env=os.getenv("APP_ENV", "production"),
            web_app_url=web_app_url.rstrip("/"),
            api_base_url=api_base_url.rstrip("/"),
            allowed_origins=allowed_origins,
            key_vault_refs_enabled=_bool_env("KEY_VAULT_REFS_ENABLED"),
            legacy_rule_folder_names=legacy_rule_folder_names,
        )


settings = Settings.from_env()

app = FastAPI(
    title="DYC Comm API",
    version="0.2.0",
    description="OAuth and runtime configuration scaffold for DYC Comm.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _cookie_secure() -> bool:
    return settings.app_env.lower() != "local"


def _code_verifier() -> str:
    return secrets.token_urlsafe(64)


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def _multi_tenant_authorize_enabled() -> bool:
    home = _normalize_id(os.getenv("MICROSOFT_ENTRA_TENANT_ID"))
    allowed = _allowed_tenant_ids()
    return bool(allowed and (allowed - {home}))


def _token_tenant_segment() -> str:
    if _multi_tenant_authorize_enabled():
        return "organizations"
    return _require_env("MICROSOFT_ENTRA_TENANT_ID")


def _login_hint_targets_home_tenant(login_hint: str | None) -> bool:
    return _normalize_id(login_hint).endswith("@danielyoung.io")


def _authorize_tenant_segment(
    login_hint: str | None = None,
    tenant_hint: str | None = None,
) -> str:
    """Pick the tenant segment for the /authorize URL.

    The primary DYC account uses the configured home tenant id so Microsoft
    can render the tenant-branded page with the known account selected.
    Hinted reconnect/connect flows for external accounts use
    ``/organizations`` when external tenants are allow-listed, so those
    specific cross-tenant accounts can still complete sign-in.
    """
    if tenant_hint:
        normalized_hint = _normalize_id(tenant_hint)
        if normalized_hint not in _allowed_tenant_ids():
            raise HTTPException(status_code=400, detail="tenant_hint is not allow-listed.")
        return normalized_hint

    if (
        login_hint
        and not _login_hint_targets_home_tenant(login_hint)
        and _multi_tenant_authorize_enabled()
    ):
        return "organizations"
    return _require_env("MICROSOFT_ENTRA_TENANT_ID")


def _authorize_url(
    state: str,
    code_challenge: str,
    login_hint: str | None = None,
    tenant_hint: str | None = None,
    prompt: str = "select_account",
    domain_hint: str | None = None,
) -> str:
    tenant_segment = _authorize_tenant_segment(login_hint=login_hint, tenant_hint=tenant_hint)
    client_id = _require_env("MICROSOFT_ENTRA_CLIENT_ID")
    redirect_uri = _require_env("MICROSOFT_ENTRA_REDIRECT_URI")
    params: dict[str, str] = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": MICROSOFT_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": prompt,
    }
    if login_hint:
        params["login_hint"] = login_hint
    if domain_hint:
        params["domain_hint"] = domain_hint
    return (
        f"https://login.microsoftonline.com/{tenant_segment}/oauth2/v2.0/authorize?"
        f"{urlencode(params)}"
    )


def _web_redirect(status: str, **params: str) -> str:
    query_items = {"auth": status}
    query_items.update(params)
    return f"{settings.web_app_url}/?{urlencode(query_items)}"


async def _exchange_code(
    code: str,
    verifier: str,
    tenant_segment: str | None = None,
) -> dict[str, Any]:
    tenant_segment = tenant_segment or _token_tenant_segment()
    client_id = _require_env("MICROSOFT_ENTRA_CLIENT_ID")
    client_secret = _require_env("MICROSOFT_ENTRA_CLIENT_SECRET")
    redirect_uri = _require_env("MICROSOFT_ENTRA_REDIRECT_URI")
    token_url = f"https://login.microsoftonline.com/{tenant_segment}/oauth2/v2.0/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": MICROSOFT_SCOPE,
        "code_verifier": verifier,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(token_url, data=payload)
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Microsoft token exchange failed",
                "status_code": response.status_code,
                "body": response.text,
            },
        )
    return response.json()


async def _graph_profile(access_token: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get("https://graph.microsoft.com/v1.0/me", headers=headers)
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Microsoft Graph profile request failed",
                "status_code": response.status_code,
                "body": response.text,
            },
        )
    return response.json()


async def _graph_get(
    access_token: str, path: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"https://graph.microsoft.com/v1.0{path}",
            headers=headers,
            params=params,
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Microsoft Graph request failed",
                "status_code": response.status_code,
                "body": response.text,
            },
        )
    return response.json()


async def _graph_get_url(access_token: str, url: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    if not url.startswith("https://graph.microsoft.com/v1.0/"):
        raise HTTPException(status_code=502, detail="Unexpected Microsoft Graph page URL.")
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, headers=headers)
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Microsoft Graph request failed",
                "status_code": response.status_code,
                "body": response.text,
            },
        )
    return response.json()


async def _graph_post(access_token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"https://graph.microsoft.com/v1.0{path}",
            headers=headers,
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Microsoft Graph write request failed",
                "status_code": response.status_code,
                "body": response.text,
            },
        )
    return response.json()


async def _graph_patch(access_token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.patch(
            f"https://graph.microsoft.com/v1.0{path}",
            headers=headers,
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Microsoft Graph write request failed",
                "status_code": response.status_code,
                "body": response.text,
            },
        )
    if not response.content:
        return {}
    return response.json()


def _session_payload(
    linked_account: dict[str, str] | None = None,
) -> dict[str, Any]:
    variables: dict[str, dict[str, bool]] = {
        name: {"present": bool(os.getenv(name)), "is_secret": is_secret}
        for name, is_secret in _RUNTIME_VARIABLES
    }
    # Required-presence check ignores optional / advisory controls so an
    # unset ALLOWED_ACCOUNT_EMAILS or unset Azure AI provider does not flag
    # config as incomplete. All vars are still surfaced under `variables`
    # so /config-check operators can see whether they are wired up.
    optional_vars = _OPTIONAL_RUNTIME_VARIABLES
    required_present = all(v["present"] for k, v in variables.items() if k not in optional_vars)
    allowed_tenant_ids = _allowed_tenant_ids()
    allow_list = {
        "tenant_allow_list_configured": bool(_split_csv_env("ALLOWED_MICROSOFT_TENANT_IDS")),
        "tenant_allow_list_count": len(allowed_tenant_ids),
        "email_allow_list_configured": bool(_allowed_account_emails()),
        "email_allow_list_count": len(_allowed_account_emails()),
        "multi_tenant_authorize": bool(
            allowed_tenant_ids - {_normalize_id(os.getenv("MICROSOFT_ENTRA_TENANT_ID"))}
        ),
    }
    ai_provider_cfg = classifier_module.AzureAIProviderConfig.from_env()
    ai_provider = {
        "selected": ai_provider_cfg.provider,
        "configured": ai_provider_cfg.is_configured(),
        "endpoint_present": bool(ai_provider_cfg.endpoint),
        "deployment_present": bool(ai_provider_cfg.deployment),
        "api_version_present": bool(ai_provider_cfg.api_version),
        "api_key_present": ai_provider_cfg.has_api_key,
    }
    return {
        "environment": settings.app_env,
        "web_app_url": settings.web_app_url,
        "api_base_url": settings.api_base_url,
        "variables": variables,
        "all_required_present": required_present,
        "has_database_url": variables["DATABASE_URL"]["present"],
        "has_entra_client_id": variables["MICROSOFT_ENTRA_CLIENT_ID"]["present"],
        "has_entra_tenant_id": variables["MICROSOFT_ENTRA_TENANT_ID"]["present"],
        "has_entra_client_secret": variables["MICROSOFT_ENTRA_CLIENT_SECRET"]["present"],
        "has_entra_redirect_uri": variables["MICROSOFT_ENTRA_REDIRECT_URI"]["present"],
        "key_vault_refs_enabled": settings.key_vault_refs_enabled,
        "auth_allow_list": allow_list,
        "ai_provider": ai_provider,
        "linked_account": linked_account,
        "mailbox_access_ready": bool(linked_account and linked_account.get("has_refresh_token")),
    }


def _folder_spec_by_name(name: str) -> dict[str, Any] | None:
    folded = _fold_name(name)
    for folder_spec in DEFAULT_MVP_FOLDER_SPECS:
        if folded == _fold_name(folder_spec["name"]):
            return folder_spec
        if any(folded == _fold_name(alias) for alias in folder_spec["aliases"]):
            return folder_spec
    return None


def _classify_folder(folder: dict[str, Any]) -> dict[str, Any]:
    display_name = str(folder.get("displayName") or "")
    folder_spec = _folder_spec_by_name(display_name)
    if folder_spec:
        return {
            "ownership": "dyc_managed",
            "routing_state": "active",
            "folder_role": folder_spec["name"],
            "is_dyc_target": True,
            "canonical_name": folder_spec["name"],
        }

    if _fold_name(display_name) in {_fold_name(name) for name in settings.legacy_rule_folder_names}:
        return {
            "ownership": "legacy_rule",
            "routing_state": "protected",
            "folder_role": "legacy_rule",
            "is_dyc_target": False,
            "canonical_name": display_name,
        }

    if _fold_name(display_name) in {_fold_name(name) for name in SYSTEM_FOLDER_NAMES}:
        return {
            "ownership": "system",
            "routing_state": "observed",
            "folder_role": "system",
            "is_dyc_target": False,
            "canonical_name": display_name,
        }

    return {
        "ownership": "manual",
        "routing_state": "observed",
        "folder_role": "manual",
        "is_dyc_target": False,
        "canonical_name": display_name,
    }


def _annotate_folders(folders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**folder, **_classify_folder(folder)} for folder in folders]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _expires_at(token_response: dict[str, Any]) -> datetime | None:
    expires_in = token_response.get("expires_in")
    if not expires_in:
        return None
    return _utcnow() + timedelta(seconds=int(expires_in))


@dataclass(frozen=True)
class AuthorizedIdentity:
    email: str
    tenant_id: str
    upn: str | None


def _authorize_callback_identity(
    token_response: dict[str, Any], profile: dict[str, Any]
) -> AuthorizedIdentity:
    """Enforce the tenant/email allow-list against the post-exchange identity.

    Raises HTTPException(403) when the identity is not allow-listed. The
    callback handler relies on this raising before any token-persistence
    code path runs, so unauthorized accounts never land in the DB.

    Identity sources, in order of trust:

    * ``tid`` and ``preferred_username`` from the ID token payload. The
      payload is read without verifying the signature, but the ID token
      itself was just delivered over TLS by Microsoft's token endpoint
      after a confidential-client exchange (client secret + PKCE), so the
      claims are treated as authorization *attributes* of the principal
      whose code we just redeemed — not as standalone proof of identity.
    * Graph ``/me`` ``mail`` / ``userPrincipalName`` for the canonical
      email when the ID token did not carry ``preferred_username``.

    A missing ``tid`` is treated as a hard failure: we cannot authorize a
    tenant we cannot identify, so the safest action is to reject.
    """
    claims = _decode_id_token_claims(token_response.get("id_token"))
    tenant_id = _normalize_id(claims.get("tid"))

    email_candidates = [
        profile.get("mail"),
        profile.get("userPrincipalName"),
        claims.get("preferred_username"),
        claims.get("upn"),
        claims.get("email"),
    ]
    email = next((value for value in email_candidates if value), None)
    if not email:
        logger.warning("auth.callback.denied reason=missing_email")
        raise HTTPException(status_code=403, detail="Sign-in denied: missing account email.")

    normalized_email = _normalize_id(email)

    if not tenant_id:
        logger.warning("auth.callback.denied reason=missing_tid email=%s", normalized_email)
        raise HTTPException(
            status_code=403,
            detail="Sign-in denied: tenant id (tid) could not be established from the token.",
        )

    allowed_tenants = _allowed_tenant_ids()
    if not allowed_tenants:
        logger.error(
            "auth.callback.denied reason=no_tenant_allow_list_configured email=%s tid=%s",
            normalized_email,
            tenant_id,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "Sign-in denied: no tenant allow-list is configured on the API. "
                "Set ALLOWED_MICROSOFT_TENANT_IDS or MICROSOFT_ENTRA_TENANT_ID."
            ),
        )

    if tenant_id not in allowed_tenants:
        logger.warning(
            "auth.callback.denied reason=tenant_not_allowed email=%s tid=%s",
            normalized_email,
            tenant_id,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Sign-in denied: tenant {tenant_id} is not allow-listed.",
        )

    allowed_emails = _allowed_account_emails()
    if allowed_emails and normalized_email not in allowed_emails:
        logger.warning(
            "auth.callback.denied reason=email_not_allowed email=%s tid=%s",
            normalized_email,
            tenant_id,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Sign-in denied: account {email} is not allow-listed.",
        )

    logger.info("auth.callback.allowed email=%s tid=%s", normalized_email, tenant_id)
    upn = claims.get("upn") or claims.get("preferred_username") or profile.get("userPrincipalName")
    return AuthorizedIdentity(email=email, tenant_id=tenant_id, upn=upn)


def _extract_account_identity(profile: dict[str, Any]) -> tuple[str, str, str]:
    email = (
        profile.get("mail") or profile.get("userPrincipalName") or profile.get("preferred_username")
    )
    if not email:
        raise HTTPException(status_code=502, detail="Missing email in Graph profile.")
    display_name = profile.get("displayName") or email
    provider_account_id = profile.get("id") or email
    return email, display_name, provider_account_id


def _psycopg():
    import psycopg

    return psycopg


def _get_connection() -> Any:
    database_url = _database_url()
    if not database_url:
        raise HTTPException(status_code=500, detail="Missing required env var: DATABASE_URL")
    psycopg = _psycopg()
    try:
        return psycopg.connect(database_url)
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to connect to PostgreSQL.",
        ) from exc


def _ensure_account_tables() -> None:
    global _DB_BOOTSTRAPPED
    if _DB_BOOTSTRAPPED or not _database_url():
        return

    with _get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS app_user (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS connected_account (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    provider_account_id TEXT NOT NULL,
                    email_address TEXT NOT NULL,
                    display_name TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    access_token TEXT,
                    refresh_token TEXT,
                    access_token_expires_at TIMESTAMPTZ,
                    token_updated_at TIMESTAMPTZ,
                    scopes TEXT[] NOT NULL DEFAULT '{}',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(provider, provider_account_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_connected_account_user
                ON connected_account(user_id)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS mailbox_folder (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES connected_account(id) ON DELETE CASCADE,
                    provider_folder_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    parent_folder_id TEXT,
                    child_folder_count INTEGER NOT NULL DEFAULT 0,
                    total_item_count INTEGER NOT NULL DEFAULT 0,
                    unread_item_count INTEGER NOT NULL DEFAULT 0,
                    is_hidden BOOLEAN NOT NULL DEFAULT false,
                    ownership TEXT NOT NULL,
                    routing_state TEXT NOT NULL,
                    folder_role TEXT NOT NULL,
                    is_dyc_target BOOLEAN NOT NULL DEFAULT false,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(account_id, provider_folder_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mailbox_folder_account
                ON mailbox_folder(account_id)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS inbox_dry_run_classification (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES connected_account(id)
                        ON DELETE CASCADE,
                    account_email TEXT NOT NULL,
                    provider_message_id TEXT NOT NULL,
                    received_at TIMESTAMPTZ,
                    sender TEXT,
                    subject TEXT,
                    current_folder TEXT,
                    recommended_folder TEXT NOT NULL,
                    category TEXT NOT NULL,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    confidence_band TEXT NOT NULL,
                    forced_review BOOLEAN NOT NULL DEFAULT false,
                    reasons TEXT[] NOT NULL DEFAULT '{}',
                    safety_flags TEXT[] NOT NULL DEFAULT '{}',
                    provider_consulted BOOLEAN NOT NULL DEFAULT false,
                    provider_name TEXT,
                    status TEXT NOT NULL DEFAULT 'classified',
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(account_id, provider_message_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbox_dry_run_account_received
                ON inbox_dry_run_classification(account_id, received_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbox_dry_run_account_email_created
                ON inbox_dry_run_classification(account_email, created_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS mailbox_move_action (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES connected_account(id)
                        ON DELETE CASCADE,
                    account_email TEXT NOT NULL,
                    provider_message_id TEXT NOT NULL,
                    source_folder_id TEXT,
                    destination_folder_id TEXT,
                    destination_folder_name TEXT NOT NULL,
                    dry_run_classification_id TEXT
                        REFERENCES inbox_dry_run_classification(id) ON DELETE SET NULL,
                    forced_review BOOLEAN NOT NULL DEFAULT false,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    categories_applied TEXT,
                    category_error TEXT,
                    requested_by_email TEXT NOT NULL,
                    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    completed_at TIMESTAMPTZ
                )
                """
            )
            cursor.execute(
                """
                ALTER TABLE mailbox_move_action
                ADD COLUMN IF NOT EXISTS categories_applied TEXT
                """
            )
            cursor.execute(
                """
                ALTER TABLE mailbox_move_action
                ADD COLUMN IF NOT EXISTS category_error TEXT
                """
            )
            cursor.execute(
                """
                ALTER TABLE mailbox_move_action
                ADD COLUMN IF NOT EXISTS sms_notified_at TIMESTAMPTZ
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mailbox_move_action_account_requested
                ON mailbox_move_action(account_id, requested_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_mailbox_move_action_account_message_status
                ON mailbox_move_action(account_id, provider_message_id, status)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS classifier_rule (
                    id TEXT PRIMARY KEY,
                    match_field TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    category TEXT NOT NULL,
                    confidence FLOAT NOT NULL DEFAULT 0.95,
                    reason TEXT,
                    enabled BOOLEAN NOT NULL DEFAULT true,
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_classifier_rule_enabled
                ON classifier_rule(enabled)
                """
            )
        connection.commit()

    _DB_BOOTSTRAPPED = True


def _persist_microsoft_account(
    profile: dict[str, Any],
    token_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    email, display_name, provider_account_id = _extract_account_identity(profile)

    if not _database_url():
        return {
            "email": email,
            "display_name": profile.get("displayName") or email,
            "provider_account_id": provider_account_id,
            "has_refresh_token": bool(token_response and token_response.get("refresh_token")),
        }

    psycopg = _psycopg()
    _ensure_account_tables()
    user_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    refresh_token = token_response.get("refresh_token") if token_response else None
    access_token = token_response.get("access_token") if token_response else None
    access_token_expires_at = _expires_at(token_response) if token_response else None

    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO app_user (id, email, display_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (email) DO UPDATE
                    SET display_name = EXCLUDED.display_name,
                        updated_at = now()
                    RETURNING id, email, display_name
                    """,
                    (user_id, email, display_name),
                )
                persisted_user_id, persisted_email, persisted_name = cursor.fetchone()
                cursor.execute(
                    """
                    INSERT INTO connected_account (
                        id,
                        user_id,
                        provider,
                        provider_account_id,
                        email_address,
                        display_name,
                        access_token,
                        refresh_token,
                        access_token_expires_at,
                        token_updated_at,
                        scopes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
                    ON CONFLICT (provider, provider_account_id) DO UPDATE
                    SET email_address = EXCLUDED.email_address,
                        display_name = EXCLUDED.display_name,
                        access_token = COALESCE(
                            EXCLUDED.access_token,
                            connected_account.access_token
                        ),
                        refresh_token = COALESCE(
                            EXCLUDED.refresh_token,
                            connected_account.refresh_token
                        ),
                        access_token_expires_at = COALESCE(
                            EXCLUDED.access_token_expires_at,
                            connected_account.access_token_expires_at
                        ),
                        token_updated_at = CASE
                            WHEN EXCLUDED.access_token IS NOT NULL
                                OR EXCLUDED.refresh_token IS NOT NULL
                                THEN now()
                            ELSE connected_account.token_updated_at
                        END,
                        scopes = EXCLUDED.scopes,
                        status = 'active',
                        updated_at = now()
                    """,
                    (
                        account_id,
                        persisted_user_id,
                        MICROSOFT_PROVIDER,
                        provider_account_id,
                        persisted_email,
                        persisted_name,
                        access_token,
                        refresh_token,
                        access_token_expires_at,
                        MICROSOFT_SCOPE.split(),
                    ),
                )
            connection.commit()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to persist linked account to PostgreSQL.",
        ) from exc

    return {
        "email": persisted_email,
        "display_name": persisted_name,
        "provider_account_id": provider_account_id,
        "has_refresh_token": bool(refresh_token),
    }


def _load_linked_account(email: str) -> dict[str, Any] | None:
    if not _database_url():
        return None

    psycopg = _psycopg()
    _ensure_account_tables()

    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        ca.email_address,
                        COALESCE(ca.display_name, au.display_name, ca.email_address),
                        ca.provider_account_id,
                        ca.refresh_token IS NOT NULL
                    FROM connected_account ca
                    JOIN app_user au ON au.id = ca.user_id
                    WHERE ca.provider = %s AND au.email = %s
                    ORDER BY ca.updated_at DESC
                    LIMIT 1
                    """,
                    (MICROSOFT_PROVIDER, email),
                )
                row = cursor.fetchone()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to load linked account from PostgreSQL.",
        ) from exc

    if not row:
        return None

    return {
        "email": row[0],
        "display_name": row[1],
        "provider_account_id": row[2],
        "has_refresh_token": row[3],
    }


def _load_account_credentials(email: str) -> dict[str, Any]:
    if not _database_url():
        raise HTTPException(
            status_code=409, detail="Database-backed mailbox access is not configured."
        )

    psycopg = _psycopg()
    _ensure_account_tables()

    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        ca.id,
                        ca.provider_account_id,
                        ca.email_address,
                        COALESCE(ca.display_name, au.display_name, ca.email_address),
                        ca.access_token,
                        ca.refresh_token,
                        ca.access_token_expires_at
                    FROM connected_account ca
                    JOIN app_user au ON au.id = ca.user_id
                    WHERE ca.provider = %s AND au.email = %s
                    ORDER BY ca.updated_at DESC
                    LIMIT 1
                    """,
                    (MICROSOFT_PROVIDER, email),
                )
                row = cursor.fetchone()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to load mailbox credentials from PostgreSQL.",
        ) from exc

    if not row:
        raise HTTPException(status_code=404, detail="No linked Microsoft account found.")

    return {
        "account_id": row[0],
        "provider_account_id": row[1],
        "email": row[2],
        "display_name": row[3],
        "access_token": row[4],
        "refresh_token": row[5],
        "access_token_expires_at": row[6],
    }


def _update_account_tokens(account_id: str, token_response: dict[str, Any]) -> None:
    psycopg = _psycopg()
    _ensure_account_tables()

    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    access_token_expires_at = _expires_at(token_response)

    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE connected_account
                    SET access_token = COALESCE(%s, access_token),
                        refresh_token = COALESCE(%s, refresh_token),
                        access_token_expires_at = %s,
                        token_updated_at = now(),
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        access_token,
                        refresh_token,
                        access_token_expires_at,
                        account_id,
                    ),
                )
            connection.commit()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to update mailbox tokens in PostgreSQL.",
        ) from exc


async def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    tenant_segment = _token_tenant_segment()
    client_id = _require_env("MICROSOFT_ENTRA_CLIENT_ID")
    client_secret = _require_env("MICROSOFT_ENTRA_CLIENT_SECRET")
    token_url = f"https://login.microsoftonline.com/{tenant_segment}/oauth2/v2.0/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": MICROSOFT_SCOPE,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(token_url, data=payload)
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Microsoft refresh token exchange failed",
                "status_code": response.status_code,
                "body": response.text,
            },
        )
    return response.json()


async def _graph_access_token_for_email(email: str) -> tuple[str, dict[str, Any]]:
    account = _load_account_credentials(email)
    access_token = account["access_token"]
    expires_at = account["access_token_expires_at"]
    if access_token and expires_at and expires_at > _utcnow() + timedelta(minutes=2):
        return access_token, account

    refresh_token = account["refresh_token"]
    if not refresh_token:
        raise HTTPException(
            status_code=409,
            detail=(
                "Linked account is missing a refresh token. "
                "Run the Microsoft sign-in flow again."
            ),
        )

    token_response = await _refresh_access_token(refresh_token)
    _update_account_tokens(account["account_id"], token_response)
    return token_response["access_token"], {
        **account,
        "access_token": token_response["access_token"],
        "refresh_token": token_response.get("refresh_token", refresh_token),
    }


async def _list_mail_folders(
    access_token: str, include_hidden: bool = False
) -> list[dict[str, Any]]:
    payload = await _graph_get(
        access_token,
        "/me/mailFolders",
        params={
            "includeHiddenFolders": str(include_hidden).lower(),
            "$select": (
                "id,displayName,parentFolderId,childFolderCount,"
                "totalItemCount,unreadItemCount,isHidden"
            ),
        },
    )
    return payload.get("value", [])


def _persist_folder_inventory(account_id: str, folders: list[dict[str, Any]]) -> None:
    if not _database_url():
        return

    _ensure_account_tables()

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                for folder in folders:
                    cursor.execute(
                        """
                        INSERT INTO mailbox_folder (
                            id,
                            account_id,
                            provider_folder_id,
                            display_name,
                            canonical_name,
                            parent_folder_id,
                            child_folder_count,
                            total_item_count,
                            unread_item_count,
                            is_hidden,
                            ownership,
                            routing_state,
                            folder_role,
                            is_dyc_target
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (account_id, provider_folder_id) DO UPDATE
                        SET display_name = EXCLUDED.display_name,
                            canonical_name = EXCLUDED.canonical_name,
                            parent_folder_id = EXCLUDED.parent_folder_id,
                            child_folder_count = EXCLUDED.child_folder_count,
                            total_item_count = EXCLUDED.total_item_count,
                            unread_item_count = EXCLUDED.unread_item_count,
                            is_hidden = EXCLUDED.is_hidden,
                            ownership = EXCLUDED.ownership,
                            routing_state = EXCLUDED.routing_state,
                            folder_role = EXCLUDED.folder_role,
                            is_dyc_target = EXCLUDED.is_dyc_target,
                            updated_at = now()
                        """,
                        (
                            str(uuid.uuid4()),
                            account_id,
                            folder.get("id"),
                            folder.get("displayName") or "",
                            folder.get("canonical_name") or folder.get("displayName") or "",
                            folder.get("parentFolderId"),
                            int(folder.get("childFolderCount") or 0),
                            int(folder.get("totalItemCount") or 0),
                            int(folder.get("unreadItemCount") or 0),
                            bool(folder.get("isHidden") or False),
                            folder.get("ownership") or "manual",
                            folder.get("routing_state") or "observed",
                            folder.get("folder_role") or "manual",
                            bool(folder.get("is_dyc_target") or False),
                        ),
                    )
            connection.commit()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to persist mailbox folder inventory to PostgreSQL.",
        ) from exc


def _load_folder_inventory(account_id: str) -> list[dict[str, Any]]:
    if not _database_url():
        return []

    _ensure_account_tables()

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        provider_folder_id,
                        display_name,
                        canonical_name,
                        parent_folder_id,
                        child_folder_count,
                        total_item_count,
                        unread_item_count,
                        is_hidden,
                        ownership,
                        routing_state,
                        folder_role,
                        is_dyc_target
                    FROM mailbox_folder
                    WHERE account_id = %s
                    ORDER BY display_name ASC
                    """,
                    (account_id,),
                )
                rows = cursor.fetchall()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to load mailbox folder inventory from PostgreSQL.",
        ) from exc

    return [
        {
            "id": row[0],
            "displayName": row[1],
            "canonical_name": row[2],
            "parentFolderId": row[3],
            "childFolderCount": row[4],
            "totalItemCount": row[5],
            "unreadItemCount": row[6],
            "isHidden": row[7],
            "ownership": row[8],
            "routing_state": row[9],
            "folder_role": row[10],
            "is_dyc_target": row[11],
        }
        for row in rows
    ]


async def _ensure_default_mail_folders(access_token: str) -> list[dict[str, Any]]:
    existing_folders = await _list_mail_folders(access_token, include_hidden=False)
    existing_by_name = {
        folder.get("displayName", "").casefold(): folder for folder in existing_folders
    }

    ensured: list[dict[str, Any]] = []
    for folder_spec in DEFAULT_MVP_FOLDER_SPECS:
        candidate_names = (folder_spec["name"], *folder_spec["aliases"])
        current = None
        for candidate_name in candidate_names:
            current = existing_by_name.get(candidate_name.casefold())
            if current:
                break
        if current:
            ensured.append(current)
            continue
        created = await _graph_post(
            access_token,
            "/me/mailFolders",
            {"displayName": folder_spec["name"]},
        )
        ensured.append(created)
    return ensured


async def _list_outlook_categories(access_token: str) -> list[dict[str, Any]]:
    payload = await _graph_get(
        access_token,
        "/me/outlook/masterCategories",
        params={"$select": "id,displayName,color"},
    )
    return payload.get("value", [])


async def _ensure_default_outlook_categories(access_token: str) -> dict[str, Any]:
    existing_categories = await _list_outlook_categories(access_token)
    existing_by_name = {
        (category.get("displayName") or "").strip().casefold(): category
        for category in existing_categories
    }

    ensured: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []
    for spec in DEFAULT_OUTLOOK_CATEGORY_SPECS:
        display_name = spec["displayName"]
        existing = existing_by_name.get(display_name.casefold())
        if existing:
            ensured.append(
                {
                    "displayName": existing.get("displayName") or display_name,
                    "color": existing.get("color"),
                    "status": "existing",
                }
            )
            continue

        created_category = await _graph_post(access_token, "/me/outlook/masterCategories", spec)
        created.append(created_category)
        ensured.append(
            {
                "displayName": created_category.get("displayName") or display_name,
                "color": created_category.get("color") or spec["color"],
                "status": "created",
            }
        )

    return {
        "expected": list(DEFAULT_OUTLOOK_CATEGORY_SPECS),
        "ensured": ensured,
        "created_count": len(created),
        "existing_count": len(ensured) - len(created),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/config-check")
def config_check() -> dict[str, Any]:
    return _session_payload()


@app.post("/classify/recommend")
async def classify_recommend(request: Request) -> dict[str, Any]:
    """Dry-run AI classification recommendation.

    This endpoint is intentionally read-only and does not move, delete, or
    send any mail. It accepts a sanitized message payload and returns the
    classifier's decision contract. Callers (or future workers) decide
    whether to act on the recommendation; v1 always requires a human in
    the loop for any mailbox-changing action.

    Request body fields (all optional):

    * ``subject`` (str)
    * ``body`` (str)
    * ``sender`` (str)
    * ``is_thread_reply`` (bool)
    * ``rule_category`` (str) — optional deterministic-rule hint
    * ``rule_confidence`` (float, 0.0–1.0)

    The response always contains ``recommended_folder``; low-confidence,
    sensitive, legal/contractual, judgment-required, short, or
    thread-flip messages are forced to ``10 - Review`` regardless of
    other signals.
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        payload = {}
    body = payload or {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object.")

    try:
        rule_confidence = float(body.get("rule_confidence", 0.0) or 0.0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="rule_confidence must be a number.") from exc

    ci = classifier_module.ClassificationInput(
        subject=str(body.get("subject") or ""),
        body=str(body.get("body") or ""),
        sender=str(body.get("sender") or ""),
        is_thread_reply=bool(body.get("is_thread_reply") or False),
        rule_category=body.get("rule_category") or None,
        rule_confidence=rule_confidence,
    )
    provider_cfg = classifier_module.AzureAIProviderConfig.from_env()
    decision = await classifier_module.classify_with_provider(ci, provider_config=provider_cfg)
    return {
        "dry_run": True,
        "recommendation": decision.to_dict(),
        "provider": {
            "selected": provider_cfg.provider,
            "configured": provider_cfg.is_configured(),
        },
        "policy_version": "v1.0",
        "review_folder": classifier_module.REVIEW_FOLDER,
    }


@app.get("/auth/session")
def auth_session(
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
    linked_name: str | None = Cookie(default=None, alias=NAME_COOKIE),
) -> dict[str, Any]:
    linked_account = None
    if linked_email:
        linked_account = _load_linked_account(linked_email) or {
            "email": linked_email,
            "display_name": linked_name or linked_email,
            "has_refresh_token": False,
        }
    return _session_payload(linked_account)


@app.get("/auth/microsoft/start")
def microsoft_start(
    login_hint: str | None = Query(default=None),
    tenant_hint: str | None = Query(default=None),
    prompt: str | None = Query(default=None),
    domain_hint: str | None = Query(default=None),
) -> Response:
    state = secrets.token_urlsafe(24)
    verifier = _code_verifier()
    hint = login_hint.strip() if login_hint else None
    tenant = tenant_hint.strip() if tenant_hint else None
    requested_prompt = prompt.strip() if prompt else "select_account"
    if requested_prompt not in {"select_account", "login"}:
        raise HTTPException(status_code=400, detail="prompt must be select_account or login.")
    domain = domain_hint.strip() if domain_hint else None
    tenant_segment = _authorize_tenant_segment(login_hint=hint or None, tenant_hint=tenant or None)
    authorize_url = _authorize_url(
        state,
        _code_challenge(verifier),
        login_hint=hint or None,
        tenant_hint=tenant or None,
        prompt=requested_prompt,
        domain_hint=domain or None,
    )
    response = RedirectResponse(authorize_url, status_code=302)
    response.set_cookie(
        key=AUTH_COOKIE,
        value=state,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=600,
    )
    response.set_cookie(
        key=PKCE_COOKIE,
        value=verifier,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=600,
    )
    response.set_cookie(
        key=AUTH_TENANT_COOKIE,
        value=tenant_segment,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=600,
    )
    return response


@app.get("/auth/microsoft/callback")
async def microsoft_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    admin_consent: str | None = Query(default=None),
    tenant: str | None = Query(default=None),
    stored_state: str | None = Cookie(default=None, alias=AUTH_COOKIE),
    stored_verifier: str | None = Cookie(default=None, alias=PKCE_COOKIE),
    stored_tenant_segment: str | None = Cookie(default=None, alias=AUTH_TENANT_COOKIE),
) -> Response:
    if error:
        description = error_description or "Microsoft returned an authorization error."
        return RedirectResponse(_web_redirect("error", reason=description), status_code=302)

    if admin_consent is not None and not code and not state:
        status = "admin_consent_success" if admin_consent.lower() == "true" else "error"
        params = {"tenant": tenant or ""}
        if status == "error":
            params["reason"] = "Microsoft admin consent was not granted."
        return RedirectResponse(_web_redirect(status, **params), status_code=302)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state.")

    if not stored_state or state != stored_state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch.")

    if not stored_verifier:
        raise HTTPException(status_code=400, detail="Missing PKCE verifier.")

    token_response = await _exchange_code(code, stored_verifier, stored_tenant_segment)
    profile = await _graph_profile(token_response["access_token"])
    # Enforce the tenant/email allow-list before any token persistence so
    # unauthorized accounts never land in the database. Raises 403 on deny.
    _authorize_callback_identity(token_response, profile)
    linked_account = _persist_microsoft_account(profile, token_response)
    email = linked_account["email"]
    display_name = linked_account["display_name"]
    redirect = RedirectResponse(
        _web_redirect("success", account=email),
        status_code=302,
    )
    redirect.set_cookie(
        key=EMAIL_COOKIE,
        value=email,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=60 * 60 * 12,
    )
    redirect.set_cookie(
        key=NAME_COOKIE,
        value=display_name,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=60 * 60 * 12,
    )
    redirect.delete_cookie(AUTH_COOKIE)
    redirect.delete_cookie(PKCE_COOKIE)
    redirect.delete_cookie(AUTH_TENANT_COOKIE)
    return redirect


@app.post("/auth/logout")
def auth_logout() -> Response:
    response = JSONResponse({"status": "signed_out"})
    response.delete_cookie(EMAIL_COOKIE)
    response.delete_cookie(NAME_COOKIE)
    response.delete_cookie(AUTH_COOKIE)
    response.delete_cookie(PKCE_COOKIE)
    response.delete_cookie(AUTH_TENANT_COOKIE)
    return response


@app.get("/mail/folders")
async def mail_folders(
    include_hidden: bool = Query(default=False),
    account: str | None = Query(
        default=None,
        description="Optional connected account email. Defaults to the signed-in mailbox.",
    ),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    if not linked_email:
        raise HTTPException(status_code=401, detail="No linked account session found.")

    target_email = _resolve_mailbox_action_email(linked_email, account)
    access_token, account = await _graph_access_token_for_email(target_email)
    folders = _annotate_folders(
        await _list_mail_folders(access_token, include_hidden=include_hidden)
    )
    return {
        "account": {
            "email": account["email"],
            "display_name": account["display_name"],
        },
        "folders": folders,
    }


@app.post("/mail/folders/bootstrap")
async def bootstrap_mail_folders(
    account: str | None = Query(
        default=None,
        description="Optional connected account email. Defaults to the signed-in mailbox.",
    ),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    if not linked_email:
        raise HTTPException(status_code=401, detail="No linked account session found.")

    target_email = _resolve_mailbox_action_email(linked_email, account)
    access_token, account = await _graph_access_token_for_email(target_email)
    folders = _annotate_folders(await _ensure_default_mail_folders(access_token))
    _persist_folder_inventory(account["account_id"], folders)
    return {
        "account": {
            "email": account["email"],
            "display_name": account["display_name"],
        },
        "ensured_folders": folders,
    }


@app.post("/mail/categories/bootstrap")
async def bootstrap_outlook_categories(
    account: str | None = Query(
        default=None,
        description="Optional connected account email. Omit to bootstrap every visible account.",
    ),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    if not linked_email:
        raise HTTPException(status_code=401, detail="No linked account session found.")

    rows = _list_user_accounts(linked_email)
    if account:
        targets = [_scope_account_to_session(rows, account)]
    else:
        targets = rows

    results: list[dict[str, Any]] = []
    for target in targets:
        email = target.get("email")
        result: dict[str, Any] = {
            "email": email,
            "display_name": target.get("display_name"),
        }
        if not target.get("has_refresh_token"):
            result.update(
                {
                    "status": "skipped",
                    "reason": "mailbox_access_not_ready",
                    "message": "Reconnect this mailbox before creating categories.",
                }
            )
            results.append(result)
            continue

        try:
            access_token, account_record = await _graph_access_token_for_email(email)
            ensured = await _ensure_default_outlook_categories(access_token)
            result.update(
                {
                    "status": "succeeded",
                    "display_name": (
                        account_record.get("display_name") or target.get("display_name")
                    ),
                    **ensured,
                }
            )
        except HTTPException as exc:
            result.update(
                {
                    "status": "failed",
                    "error": exc.detail,
                }
            )
        results.append(result)

    return {
        "categories": list(DEFAULT_OUTLOOK_CATEGORY_SPECS),
        "accounts": results,
    }


@app.post("/mail/folders/inventory/sync")
async def sync_mail_folder_inventory(
    include_hidden: bool = Query(default=True),
    account: str | None = Query(
        default=None,
        description="Optional connected account email. Defaults to the signed-in mailbox.",
    ),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    if not linked_email:
        raise HTTPException(status_code=401, detail="No linked account session found.")

    target_email = _resolve_mailbox_action_email(linked_email, account)
    access_token, account = await _graph_access_token_for_email(target_email)
    folders = _annotate_folders(
        await _list_mail_folders(access_token, include_hidden=include_hidden)
    )
    _persist_folder_inventory(account["account_id"], folders)
    return {
        "account": {
            "email": account["email"],
            "display_name": account["display_name"],
        },
        "folders": folders,
    }


@app.get("/mail/folders/inventory")
async def mail_folder_inventory(
    account: str | None = Query(
        default=None,
        description="Optional connected account email. Defaults to the signed-in mailbox.",
    ),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    if not linked_email:
        raise HTTPException(status_code=401, detail="No linked account session found.")

    target_email = _resolve_mailbox_action_email(linked_email, account)
    account = _load_account_credentials(target_email)
    folders = _load_folder_inventory(account["account_id"])
    return {
        "account": {
            "email": account["email"],
            "display_name": account["display_name"],
        },
        "folders": folders,
    }


# =========================================================================
# Operations dashboard
# =========================================================================
#
# The dashboard endpoints expose only metrics that can be honestly computed
# from the current data model. Anything that requires ingestion or audit
# tables that have not yet been populated is reported as `available: false`
# with a `reason` explaining what instrumentation is missing. Do not fabricate
# data here — surface honest empty states until the backing pipeline lands.
# See docs/dashboard-instrumentation.md for the next instrumentation slice.

PENDING_REASON_INGESTION = (
    "Email ingestion is not yet implemented; no email_message rows are written. "
    "Next step: connector worker that persists message metadata and timestamps."
)
PENDING_REASON_ACTIONS = (
    "Aggregate action metrics are not yet wired. Approved inbox moves are "
    "logged in mailbox_move_action and shown in /activity; the broader "
    "mailbox_action/audit_event pipeline is still pending."
)


def _list_user_accounts(user_email: str) -> list[dict[str, Any]]:
    if not _database_url():
        return []

    psycopg = _psycopg()
    _ensure_account_tables()
    visible_emails = _visible_account_emails_for_session(user_email)

    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        ca.id,
                        ca.provider,
                        ca.email_address,
                        COALESCE(ca.display_name, au.display_name, ca.email_address),
                        ca.status,
                        ca.refresh_token IS NOT NULL,
                        ca.token_updated_at,
                        ca.updated_at,
                        ca.created_at
                    FROM connected_account ca
                    JOIN app_user au ON au.id = ca.user_id
                    WHERE au.email = %s
                       OR lower(ca.email_address) = ANY(%s)
                    ORDER BY ca.updated_at DESC
                    """,
                    (user_email, visible_emails),
                )
                rows = cursor.fetchall()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to load connected accounts from PostgreSQL.",
        ) from exc

    return [
        {
            "account_id": row[0],
            "provider": row[1],
            "email": row[2],
            "display_name": row[3],
            "status": row[4],
            "has_refresh_token": row[5],
            "token_updated_at": row[6].isoformat() if row[6] else None,
            "updated_at": row[7].isoformat() if row[7] else None,
            "created_at": row[8].isoformat() if row[8] else None,
        }
        for row in rows
    ]


def _list_automation_accounts() -> list[dict[str, Any]]:
    """Return connected accounts eligible for the machine scheduler to inspect."""
    allowed_emails = _allowed_account_emails()
    if not _database_url() or not allowed_emails:
        return []

    psycopg = _psycopg()
    _ensure_account_tables()

    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        ca.id,
                        ca.provider,
                        ca.email_address,
                        COALESCE(ca.display_name, au.display_name, ca.email_address),
                        ca.status,
                        ca.refresh_token IS NOT NULL,
                        ca.token_updated_at,
                        ca.updated_at,
                        ca.created_at
                    FROM connected_account ca
                    JOIN app_user au ON au.id = ca.user_id
                    WHERE lower(ca.email_address) = ANY(%s)
                      AND ca.status = 'active'
                    ORDER BY ca.updated_at DESC
                    """,
                    (sorted(allowed_emails),),
                )
                rows = cursor.fetchall()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to load automation accounts from PostgreSQL.",
        ) from exc

    return [
        {
            "account_id": row[0],
            "provider": row[1],
            "email": row[2],
            "display_name": row[3],
            "status": row[4],
            "has_refresh_token": row[5],
            "token_updated_at": row[6].isoformat() if row[6] else None,
            "updated_at": row[7].isoformat() if row[7] else None,
            "created_at": row[8].isoformat() if row[8] else None,
        }
        for row in rows
    ]


def _summarize_folder_inventory(account_id: str) -> dict[str, Any]:
    folders = _load_folder_inventory(account_id)
    total = len(folders)
    by_ownership: dict[str, int] = {}
    dyc_targets = 0
    for folder in folders:
        ownership = folder.get("ownership") or "unknown"
        by_ownership[ownership] = by_ownership.get(ownership, 0) + 1
        if folder.get("is_dyc_target"):
            dyc_targets += 1
    return {
        "available": True,
        "total_folders": total,
        "dyc_target_folders": dyc_targets,
        "by_ownership": by_ownership,
        "expected_dyc_target_count": len(DEFAULT_MVP_FOLDER_SPECS),
        "is_bootstrapped": dyc_targets >= len(DEFAULT_MVP_FOLDER_SPECS),
    }


def _empty_volume_metrics() -> dict[str, Any]:
    return {
        "available": False,
        "reason": PENDING_REASON_INGESTION,
        "window_days": 7,
        "messages_in": None,
        "messages_processed": None,
        "errors": None,
        "by_day": [],
    }


def _empty_action_metrics() -> dict[str, Any]:
    return {
        "available": False,
        "reason": PENDING_REASON_ACTIONS,
        "window_days": 7,
        "actions_recommended": None,
        "actions_executed": None,
        "actions_failed": None,
        "last_action_at": None,
    }


def _automation_health_for_account(account_row: dict[str, Any]) -> dict[str, Any]:
    if not account_row.get("account_id"):
        return {
            "state": "red",
            "label": "Not persisted",
            "automation_ready": False,
            "reasons": ["Account is session-only; reconnect so mailbox tokens are stored."],
        }
    if not account_row.get("has_refresh_token"):
        return {
            "state": "red",
            "label": "Reconnect required",
            "automation_ready": False,
            "reasons": ["Mailbox access token is missing or expired."],
        }

    inventory = _summarize_folder_inventory(account_row["account_id"])
    if (inventory.get("dyc_target_folders") or 0) == 0:
        return {
            "state": "red",
            "label": "Folders missing",
            "automation_ready": False,
            "reasons": ["No DYC-managed target folders are inventoried; run Bootstrap."],
        }
    if not inventory.get("is_bootstrapped"):
        return {
            "state": "yellow",
            "label": "Folders incomplete",
            "automation_ready": True,
            "reasons": ["One or more DYC-managed target folders are missing; run Bootstrap."],
        }
    return {
        "state": "green",
        "label": "Automation ready",
        "automation_ready": True,
        "reasons": ["Mailbox access and DYC-managed folders are ready."],
    }


def _account_dashboard_payload(account_row: dict[str, Any]) -> dict[str, Any]:
    folder_summary = _summarize_folder_inventory(account_row["account_id"])
    automation_health = _automation_health_for_account(account_row)
    return {
        "account": {
            "account_id": account_row["account_id"],
            "provider": account_row["provider"],
            "email": account_row["email"],
            "display_name": account_row["display_name"],
            "status": account_row["status"],
            "mailbox_access_ready": bool(account_row["has_refresh_token"]),
            "token_updated_at": account_row["token_updated_at"],
            "created_at": account_row["created_at"],
            "updated_at": account_row["updated_at"],
        },
        "folder_inventory": folder_summary,
        "automation_health": automation_health,
        "email_volume": _empty_volume_metrics(),
        "action_activity": _empty_action_metrics(),
    }


def _resolve_session_user_email(linked_email: str | None) -> str:
    if not linked_email:
        raise HTTPException(status_code=401, detail="No linked account session found.")
    return linked_email


@app.get("/accounts")
def list_accounts(
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
    linked_name: str | None = Cookie(default=None, alias=NAME_COOKIE),
) -> dict[str, Any]:
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)
    if rows:
        accounts = [
            {
                "account_id": row["account_id"],
                "provider": row["provider"],
                "email": row["email"],
                "display_name": row["display_name"],
                "status": row["status"],
                "mailbox_access_ready": bool(row["has_refresh_token"]),
                "automation_health": _automation_health_for_account(row),
                "token_updated_at": row["token_updated_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
    else:
        # No DB persistence — fall back to the cookie-only view so callers can
        # still see the active session account without inventing data.
        accounts = [
            {
                "account_id": None,
                "provider": MICROSOFT_PROVIDER,
                "email": user_email,
                "display_name": linked_name or user_email,
                "status": "session_only",
                "mailbox_access_ready": False,
                "automation_health": {
                    "state": "red",
                    "label": "Reconnect required",
                    "automation_ready": False,
                    "reasons": ["Account is session-only; reconnect so mailbox tokens are stored."],
                },
                "token_updated_at": None,
                "created_at": None,
                "updated_at": None,
            }
        ]
    return {
        "user": {"email": user_email},
        "accounts": accounts,
    }


@app.get("/dashboard/summary")
def dashboard_summary(
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
    linked_name: str | None = Cookie(default=None, alias=NAME_COOKIE),
) -> dict[str, Any]:
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)

    if rows:
        per_account = [_account_dashboard_payload(row) for row in rows]
    else:
        # No persisted accounts for this user — describe the session-only view
        # so the dashboard can render an honest "linked but not persisted"
        # state instead of fabricating numbers.
        per_account = [
            {
                "account": {
                    "account_id": None,
                    "provider": MICROSOFT_PROVIDER,
                    "email": user_email,
                    "display_name": linked_name or user_email,
                    "status": "session_only",
                    "mailbox_access_ready": False,
                    "automation_health": {
                        "state": "red",
                        "label": "Reconnect required",
                        "automation_ready": False,
                        "reasons": [
                            "Account is session-only; reconnect so mailbox tokens are stored."
                        ],
                    },
                    "token_updated_at": None,
                    "created_at": None,
                    "updated_at": None,
                },
                "folder_inventory": {
                    "available": False,
                    "reason": (
                        "No connected_account row for this session — DATABASE_URL "
                        "may not be configured, or the OAuth flow has not run "
                        "with persistence enabled."
                    ),
                    "total_folders": 0,
                    "dyc_target_folders": 0,
                    "by_ownership": {},
                    "expected_dyc_target_count": len(DEFAULT_MVP_FOLDER_SPECS),
                    "is_bootstrapped": False,
                },
                "email_volume": _empty_volume_metrics(),
                "action_activity": _empty_action_metrics(),
            }
        ]

    totals = {
        "connected_accounts": len(per_account),
        "mailbox_ready_accounts": sum(
            1 for entry in per_account if entry["account"]["mailbox_access_ready"]
        ),
        "total_folders": sum(
            entry["folder_inventory"].get("total_folders") or 0 for entry in per_account
        ),
        "dyc_target_folders": sum(
            entry["folder_inventory"].get("dyc_target_folders") or 0 for entry in per_account
        ),
    }

    return {
        "generated_at": _utcnow().isoformat(),
        "user": {"email": user_email},
        "totals": totals,
        "accounts": per_account,
        "pending_instrumentation": [
            {
                "metric": "email_volume",
                "reason": PENDING_REASON_INGESTION,
            },
            {
                "metric": "action_activity",
                "reason": PENDING_REASON_ACTIONS,
            },
        ],
    }


# =========================================================================
# Activity log and alerts
# =========================================================================
#
# These endpoints expose only honestly-derivable signals. The activity log
# surfaces folder-inventory changes and approved inbox moves when those rows
# exist. Alerts are computed from current state — no fabricated/example
# notices.

PENDING_REASON_MESSAGE_MOVEMENT = (
    "No persisted account rows are available for this session, so message "
    "movement cannot be queried yet. Approved inbox moves are logged once "
    "a database-backed account exists and a Move action is run."
)


def _load_folder_activity(account_id: str, limit: int = 25) -> list[dict[str, Any]]:
    if not _database_url():
        return []

    _ensure_account_tables()

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        provider_folder_id,
                        display_name,
                        canonical_name,
                        ownership,
                        is_dyc_target,
                        created_at,
                        updated_at
                    FROM mailbox_folder
                    WHERE account_id = %s
                    ORDER BY GREATEST(updated_at, created_at) DESC
                    LIMIT %s
                    """,
                    (account_id, limit),
                )
                rows = cursor.fetchall()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to load mailbox folder activity from PostgreSQL.",
        ) from exc

    events: list[dict[str, Any]] = []
    for row in rows:
        provider_folder_id = row[0]
        display_name = row[1]
        canonical_name = row[2]
        ownership = row[3]
        is_dyc_target = row[4]
        created_at = row[5]
        updated_at = row[6]
        is_initial = (
            updated_at is None
            or created_at is None
            or abs((updated_at - created_at).total_seconds()) < 1.0
        )
        timestamp = updated_at or created_at
        events.append(
            {
                "event_type": "folder.bootstrap" if is_initial else "folder.sync",
                "occurred_at": timestamp.isoformat() if timestamp else None,
                "folder": {
                    "provider_folder_id": provider_folder_id,
                    "display_name": display_name,
                    "canonical_name": canonical_name,
                    "ownership": ownership,
                    "is_dyc_target": bool(is_dyc_target),
                },
            }
        )
    return events


def _filter_rows_by_account(
    rows: list[dict[str, Any]], account: str | None
) -> list[dict[str, Any]]:
    if not account:
        return rows
    needle = account.strip().lower()
    if not needle:
        return rows
    return [row for row in rows if (row.get("email") or "").lower() == needle]


@app.get("/activity")
def activity_log(
    limit: int = Query(default=25, ge=1, le=100),
    account: str | None = Query(default=None),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)
    scoped_rows = _filter_rows_by_account(rows, account)
    if account and not scoped_rows:
        raise HTTPException(
            status_code=404,
            detail="No connected account with that email is linked to this session.",
        )

    folder_events: list[dict[str, Any]] = []
    folder_available = False
    move_events: list[dict[str, Any]] = []
    move_available = False
    for row in scoped_rows:
        events = _load_folder_activity(row["account_id"], limit=limit)
        for event in events:
            event["account"] = {
                "account_id": row["account_id"],
                "email": row["email"],
            }
            folder_events.append(event)
        folder_available = True

        for action in _load_move_actions(row["account_id"], limit=limit):
            move_events.append(
                {
                    "event_type": f"message.move.{action['status']}",
                    "occurred_at": action["completed_at"] or action["requested_at"],
                    "account": {
                        "account_id": row["account_id"],
                        "email": row["email"],
                    },
                    "move": {
                        "provider_message_id": action["provider_message_id"],
                        "source_folder_id": action["source_folder_id"],
                        "destination_folder_id": action["destination_folder_id"],
                        "destination_folder_name": action["destination_folder_name"],
                        "forced_review": action["forced_review"],
                        "status": action["status"],
                        "error": action["error"],
                        "categories_applied": action["categories_applied"],
                        "category_error": action["category_error"],
                        "requested_by_email": action["requested_by_email"],
                        "requested_at": action["requested_at"],
                        "completed_at": action["completed_at"],
                    },
                }
            )
        move_available = True

    folder_events.sort(
        key=lambda event: event.get("occurred_at") or "",
        reverse=True,
    )
    folder_events = folder_events[:limit]

    move_events.sort(
        key=lambda event: event.get("occurred_at") or "",
        reverse=True,
    )
    move_events = move_events[:limit]

    folder_reason: str | None = None
    if not folder_available:
        folder_reason = (
            "No connected_account rows for this session; folder activity "
            "becomes available once accounts are persisted via the OAuth "
            "callback with DATABASE_URL configured."
        )

    move_reason: str | None = None
    if not move_available:
        move_reason = PENDING_REASON_MESSAGE_MOVEMENT
    elif not move_events:
        move_reason = (
            "No approved-move actions have been recorded yet for this "
            "account. Use the Move action on the Inbox Sorting tab after "
            "running the dry-run to populate this feed."
        )

    return {
        "generated_at": _utcnow().isoformat(),
        "user": {"email": user_email},
        "scope": {"account": account} if account else {"account": None},
        "folder_activity": {
            "available": folder_available,
            "reason": folder_reason,
            "events": folder_events,
        },
        "message_movement": {
            "available": move_available,
            "reason": move_reason,
            "events": move_events,
        },
        "pending_instrumentation": (
            []
            if move_available
            else [
                {
                    "metric": "message_movement",
                    "reason": PENDING_REASON_MESSAGE_MOVEMENT,
                }
            ]
        ),
    }


def _compute_alerts(
    accounts: list[dict[str, Any]],
    runtime_present: bool,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []

    if not runtime_present:
        alerts.append(
            {
                "code": "runtime_config_missing",
                "severity": "error",
                "message": (
                    "One or more required runtime variables are missing on the API. "
                    "Mailbox sign-in cannot complete until they are populated."
                ),
                "next_action": (
                    "Open the API/Diagnostics tab and populate any variables marked "
                    "missing in the runtime config check."
                ),
            }
        )

    if not accounts:
        alerts.append(
            {
                "code": "no_connected_accounts",
                "severity": "warning",
                "message": "No Microsoft 365 accounts are connected to this user.",
                "next_action": (
                    "Use the Connect Microsoft 365 card on the Dashboard to link "
                    "an account (e.g. daniel.young@digitalhealthworks.com)."
                ),
            }
        )

    for account in accounts:
        if not account.get("mailbox_access_ready"):
            alerts.append(
                {
                    "code": "mailbox_access_not_ready",
                    "severity": "warning",
                    "message": (
                        f"Account {account.get('email')} is linked but mailbox "
                        "access is not ready (no refresh token or session-only)."
                    ),
                    "next_action": (
                        "Re-run Microsoft sign-in for this account so a refresh token is stored."
                    ),
                    "context": {"email": account.get("email")},
                }
            )

    if not _database_url():
        alerts.append(
            {
                "code": "database_unavailable",
                "severity": "warning",
                "message": (
                    "DATABASE_URL is not configured; account and folder rows are "
                    "not persisted. The dashboard is running in session-only mode."
                ),
                "next_action": (
                    "Set DATABASE_URL on the API runtime so connected_account and "
                    "mailbox_folder rows are written by the OAuth and inventory "
                    "flows."
                ),
            }
        )
    else:
        for account in accounts:
            account_id = account.get("account_id")
            if not account_id:
                continue
            inventory = _summarize_folder_inventory(account_id)
            if (inventory.get("total_folders") or 0) == 0:
                alerts.append(
                    {
                        "code": "folder_inventory_missing",
                        "severity": "info",
                        "message": (
                            "No folder inventory has been synced for "
                            f"{account.get('email')} yet."
                        ),
                        "next_action": (
                            "Run Bootstrap or Inventory Sync from the API/Diagnostics "
                            "tab to populate mailbox_folder rows."
                        ),
                        "context": {"email": account.get("email")},
                    }
                )
            elif not inventory.get("is_bootstrapped"):
                alerts.append(
                    {
                        "code": "folder_inventory_incomplete",
                        "severity": "info",
                        "message": (
                            f"{account.get('email')} is missing one or more "
                            "default DYC-managed folders."
                        ),
                        "next_action": (
                            "Run Bootstrap from the API/Diagnostics tab to create "
                            "the missing default folders."
                        ),
                        "context": {"email": account.get("email")},
                    }
                )

    return alerts


@app.get("/alerts")
def alerts(
    account: str | None = Query(default=None),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)

    if account:
        scoped_rows = _filter_rows_by_account(rows, account)
        if not scoped_rows:
            raise HTTPException(
                status_code=404,
                detail="No connected account with that email is linked to this session.",
            )
        accounts = [
            {
                "account_id": row["account_id"],
                "email": row["email"],
                "mailbox_access_ready": bool(row["has_refresh_token"]),
            }
            for row in scoped_rows
        ]
    elif rows:
        accounts = [
            {
                "account_id": row["account_id"],
                "email": row["email"],
                "mailbox_access_ready": bool(row["has_refresh_token"]),
            }
            for row in rows
        ]
    else:
        accounts = [
            {
                "account_id": None,
                "email": user_email,
                "mailbox_access_ready": False,
            }
        ]

    runtime_present = all(
        bool(os.getenv(name))
        for name, _ in _RUNTIME_VARIABLES
        if name not in _OPTIONAL_RUNTIME_VARIABLES
    )

    items = _compute_alerts(accounts, runtime_present)
    counts = {"error": 0, "warning": 0, "info": 0}
    for item in items:
        severity = item.get("severity", "info")
        counts[severity] = counts.get(severity, 0) + 1

    return {
        "generated_at": _utcnow().isoformat(),
        "user": {"email": user_email},
        "scope": {"account": account} if account else {"account": None},
        "counts": counts,
        "alerts": items,
    }


# =========================================================================
# Inbox dry-run classification
# =========================================================================
#
# These endpoints fetch a small recent batch of inbox messages for an
# allow-listed account, run them through the existing dry-run classifier
# (apps.api.app.classifier), and persist the recommendation for operator
# review. They do NOT move, delete, send, or otherwise mutate the mailbox.
# Only the message metadata listed in the migration is stored — body
# content is discarded after classification.

INBOX_DRY_RUN_DEFAULT_LIMIT = 25
INBOX_DRY_RUN_MAX_LIMIT = 100
INBOX_DRY_RUN_BODY_PREVIEW_CHARS = 4000


def _scope_account_to_session(rows: list[dict[str, Any]], requested_email: str) -> dict[str, Any]:
    """Pick the connected_account row matching ``requested_email``.

    Raises 404 if the requested email is not linked to this session. This
    is the safety boundary that prevents an operator from running the
    dry-run against an account they did not connect.
    """
    needle = requested_email.strip().lower()
    if not needle:
        raise HTTPException(status_code=400, detail="account email is required.")
    target = next((row for row in rows if (row.get("email") or "").lower() == needle), None)
    if not target:
        raise HTTPException(
            status_code=404,
            detail="No connected account with that email is linked to this session.",
        )
    return target


def _resolve_mailbox_action_email(linked_email: str, requested_email: str | None) -> str:
    """Resolve an optional account query param within the current session scope."""
    if not requested_email:
        return linked_email

    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)
    target = _scope_account_to_session(rows, requested_email)
    return target["email"]


async def _list_inbox_messages(access_token: str, limit: int) -> list[dict[str, Any]]:
    """Read-only fetch of the most recent inbox messages from Microsoft Graph.

    Uses GET /me/mailFolders/inbox/messages with a small $top and $select
    so we only request the metadata we will store. Never issues writes.
    """
    payload = await _graph_get(
        access_token,
        "/me/mailFolders/inbox/messages",
        params={
            "$top": str(limit),
            "$orderby": "receivedDateTime desc",
            "$select": (
                "id,receivedDateTime,subject,from,sender,parentFolderId," "bodyPreview,categories"
            ),
        },
    )
    return payload.get("value", [])


async def _list_inbox_messages_paginated(
    access_token: str, scan_limit: int
) -> list[dict[str, Any]]:
    """Fetch a bounded page-walk of recent Inbox messages from Microsoft Graph."""
    messages: list[dict[str, Any]] = []
    remaining = scan_limit
    page_size = min(50, remaining)
    payload = await _graph_get(
        access_token,
        "/me/mailFolders/inbox/messages",
        params={
            "$top": str(page_size),
            "$orderby": "receivedDateTime desc",
            "$select": (
                "id,receivedDateTime,subject,from,sender,parentFolderId," "bodyPreview,categories"
            ),
        },
    )

    while True:
        page_values = payload.get("value", [])
        messages.extend(page_values[:remaining])
        remaining = scan_limit - len(messages)
        if remaining <= 0:
            break

        next_link = payload.get("@odata.nextLink")
        if not next_link:
            break
        payload = await _graph_get_url(access_token, str(next_link))

    return messages


def _coerce_received_at(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        # Graph returns ISO-8601 with a trailing Z; datetime.fromisoformat
        # in 3.12 understands the 'Z' suffix when it is replaced with +00:00.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _sender_email_from_graph(message: dict[str, Any]) -> str:
    for key in ("from", "sender"):
        block = message.get(key) or {}
        addr = (block.get("emailAddress") or {}).get("address")
        if addr:
            return addr
    return ""


def _load_classifier_rules() -> list[dict[str, Any]]:
    if not _database_url():
        return []
    _ensure_account_tables()
    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, match_field, pattern, category, confidence,
                           reason, enabled, created_by, created_at
                    FROM classifier_rule
                    ORDER BY created_at ASC
                    """
                )
                rows = cursor.fetchall()
    except psycopg.Error as exc:
        raise HTTPException(status_code=502, detail="Unable to load classifier rules.") from exc
    return [
        {
            "id": row[0],
            "match_field": row[1],
            "pattern": row[2],
            "category": row[3],
            "confidence": float(row[4]),
            "reason": row[5] or "",
            "enabled": bool(row[6]),
            "created_by": row[7],
            "created_at": row[8].isoformat() if row[8] else None,
        }
        for row in rows
    ]


def _create_classifier_rule(
    match_field: str,
    pattern: str,
    category: str,
    confidence: float,
    reason: str,
    created_by: str,
) -> dict[str, Any]:
    _ensure_account_tables()
    rule_id = str(uuid.uuid4())
    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO classifier_rule
                        (id, match_field, pattern, category, confidence, reason, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, match_field, pattern, category, confidence,
                              reason, enabled, created_by, created_at
                    """,
                    (
                        rule_id,
                        match_field,
                        pattern,
                        category,
                        confidence,
                        reason or None,
                        created_by,
                    ),
                )
                row = cursor.fetchone()
            connection.commit()
    except psycopg.Error as exc:
        raise HTTPException(status_code=502, detail="Unable to create classifier rule.") from exc
    return {
        "id": row[0],
        "match_field": row[1],
        "pattern": row[2],
        "category": row[3],
        "confidence": float(row[4]),
        "reason": row[5] or "",
        "enabled": bool(row[6]),
        "created_by": row[7],
        "created_at": row[8].isoformat() if row[8] else None,
    }


def _delete_classifier_rule(rule_id: str) -> bool:
    if not _database_url():
        return False
    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM classifier_rule WHERE id = %s",
                    (rule_id,),
                )
                deleted = cursor.rowcount > 0
            connection.commit()
    except psycopg.Error as exc:
        raise HTTPException(status_code=502, detail="Unable to delete classifier rule.") from exc
    return deleted


def _set_classifier_rule_enabled(rule_id: str, enabled: bool) -> bool:
    if not _database_url():
        return False
    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE classifier_rule SET enabled = %s WHERE id = %s",
                    (enabled, rule_id),
                )
                updated = cursor.rowcount > 0
            connection.commit()
    except psycopg.Error as exc:
        raise HTTPException(status_code=502, detail="Unable to update classifier rule.") from exc
    return updated


def _deterministic_rule_for_message(
    *,
    subject: str,
    sender: str,
    body_preview: str,
) -> tuple[str | None, float, str | None]:
    """Return a conservative local category hint for obvious machine mail.

    This is not an AI substitute. It only handles high-signal operational
    patterns where the sender/subject already identify the message type.
    Anything ambiguous still falls through to the classifier's Review default.
    """
    sender_l = sender.casefold()
    subject_l = subject.casefold()
    combined = f"{subject}\n{body_preview}".casefold()

    for rule in _load_classifier_rules():
        if not rule.get("enabled"):
            continue
        field = rule["match_field"]
        pattern = rule["pattern"].casefold()
        if field == "sender":
            target = sender_l
        elif field == "subject":
            target = subject_l
        elif field == "body":
            target = body_preview.casefold()
        else:
            target = combined
        if pattern in target:
            return rule["category"], float(rule["confidence"]), rule.get("reason") or "user rule"

    if (
        "github.com" in sender_l
        or "[mrdanielyoung/dyc-comm]" in subject_l
        or "pr run failed" in subject_l
        or "workflow run" in subject_l
        or "ci -" in subject_l
    ):
        return "it_reports", 0.95, "github/ci notification"

    if sender_l == "team@mail.perplexity.ai" and (
        "your task is complete" in subject_l
        or "repository" in subject_l
        or "review of" in subject_l
    ):
        return "it_reports", 0.95, "perplexity task/repository notification"

    if "linkedin.com" in sender_l:
        return "social_media", 0.99, "linkedin notification"

    if "pipedrive.com" in sender_l:
        return "sales_crm", 0.99, "pipedrive crm notification"

    # DMARC and email-authentication reports (aggregate, failure, forensic).
    # Matches both Microsoft format ("DMARC report") and Google/RFC-7489
    # format ("Report domain: X Submitter: Y Report-ID: Z").
    if "dmarc" in sender_l or any(
        phrase in combined
        for phrase in (
            "dmarc aggregate report",
            "dmarc report",
            "dmarc failure report",
            "dmarc ruf",
            "dmarc rua",
            "spf failure report",
            "dkim failure report",
            "report domain:",
            "submitter: google.com",
            "submitter: microsoft.com",
            "submitter: yahoo.com",
        )
    ):
        return "it_reports", 0.95, "dmarc/email-auth report"

    # Autonomous notifications from IT services, providers, and
    # technology-related monitoring / infrastructure tools.
    if any(
        phrase in combined
        for phrase in (
            "ssl certificate",
            "certificate expir",
            "tls certificate",
            "uptime report",
            "downtime alert",
            "server alert",
            "backup report",
            "backup completed",
            "backup failed",
            "security report",
            "vulnerability report",
            "patch report",
            "system report",
            "infrastructure report",
            "monitoring alert",
            "disk usage",
            "memory usage",
            "cpu usage",
            "azure monitor",
            "azure security",
            "azure advisor",
            "microsoft defender",
            "microsoft secure score",
            "office 365 message center",
            "microsoft 365 message center",
            "service health",
            "service degradation",
            "incident report",
            "postmaster",
            "mail delivery",
            "delivery status",
            "spam report",
            "abuse report",
        )
    ):
        return "it_reports", 0.92, "it service/infrastructure notification"

    if any(
        token in combined
        for token in (
            "pull request",
            "repository",
            "build failed",
            "deployment failed",
            "ci failed",
            "run failed",
        )
    ):
        return "it_reports", 0.85, "repository/build notification"

    # Billing, invoices, and payment receipts. Broad match on common
    # transactional subjects across all three inboxes (EN + DE).
    if any(
        phrase in combined
        for phrase in (
            "your receipt",
            "purchase receipt",
            "payment receipt",
            "your invoice",
            "invoice is available",
            "invoice for",
            "your bill",
            "your statement",
            "statement is available",
            "statement is now available",
            "monatsabrechnung",
            "ihre rechnung",
            "zahlungsbestätigung",
            "received payment",
            "received $",
            "received €",
            "transfer completed",
            "payment for",
            "subscription renewal",
            "will debit your account",
            "direct debit",
            "lastschrift",
        )
    ):
        return "finance_money", 0.97, "billing/payment/invoice notification"

    # Marketing newsletters and promotional emails (EN + DE).
    if any(
        phrase in combined
        for phrase in (
            "rabatt sichern",
            "exklusives angebot",
            "angebot:",
            "% off",
            "% rabatt",
            "unsubscribe",
            "abmelden",
            "view in browser",
            "im browser anzeigen",
        )
    ):
        return "marketing_promotions", 0.95, "promotional/marketing email"

    # News digests and newsletters.
    if any(
        phrase in combined
        for phrase in (
            "daily news:",
            "daily digest",
            "weekly digest",
            "weekly roundup",
            "newsletter",
            "digest:",
            "bulletin:",
            " bulletin",
            "online first:",
        )
    ):
        return "newsletters_news", 0.95, "news digest or newsletter"

    if any(
        token in sender_l
        for token in (
            "noreply",
            "no-reply",
            "notification",
            "notifications",
        )
    ):
        return "notifications_system", 0.78, "machine notification sender"

    return None, 0.0, None


def _classification_input_from_message(
    message: dict[str, Any],
) -> classifier_module.ClassificationInput:
    subject = str(message.get("subject") or "")
    body_preview = str(message.get("bodyPreview") or "")[:INBOX_DRY_RUN_BODY_PREVIEW_CHARS]
    sender = _sender_email_from_graph(message)
    rule_category, rule_confidence, _rule_reason = _deterministic_rule_for_message(
        subject=subject,
        sender=sender,
        body_preview=body_preview,
    )
    return classifier_module.ClassificationInput(
        subject=subject,
        body=body_preview,
        sender=sender,
        is_thread_reply=False,
        rule_category=rule_category,
        rule_confidence=rule_confidence,
    )


def _persist_dry_run_classification(
    account_id: str,
    account_email: str,
    message: dict[str, Any],
    decision: classifier_module.ClassificationDecision,
    provider_cfg: classifier_module.AzureAIProviderConfig | None,
    status: str,
    error: str | None = None,
) -> None:
    if not _database_url():
        return

    psycopg = _psycopg()
    _ensure_account_tables()

    received_at = _coerce_received_at(message.get("receivedDateTime"))
    provider_message_id = str(message.get("id") or "")
    if not provider_message_id:
        return

    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO inbox_dry_run_classification (
                        id,
                        account_id,
                        account_email,
                        provider_message_id,
                        received_at,
                        sender,
                        subject,
                        current_folder,
                        recommended_folder,
                        category,
                        confidence,
                        confidence_band,
                        forced_review,
                        reasons,
                        safety_flags,
                        provider_consulted,
                        provider_name,
                        status,
                        error
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (account_id, provider_message_id) DO UPDATE
                    SET received_at = EXCLUDED.received_at,
                        sender = EXCLUDED.sender,
                        subject = EXCLUDED.subject,
                        current_folder = EXCLUDED.current_folder,
                        recommended_folder = EXCLUDED.recommended_folder,
                        category = EXCLUDED.category,
                        confidence = EXCLUDED.confidence,
                        confidence_band = EXCLUDED.confidence_band,
                        forced_review = EXCLUDED.forced_review,
                        reasons = EXCLUDED.reasons,
                        safety_flags = EXCLUDED.safety_flags,
                        provider_consulted = EXCLUDED.provider_consulted,
                        provider_name = EXCLUDED.provider_name,
                        status = EXCLUDED.status,
                        error = EXCLUDED.error,
                        created_at = now()
                    """,
                    (
                        str(uuid.uuid4()),
                        account_id,
                        account_email,
                        provider_message_id,
                        received_at,
                        _sender_email_from_graph(message),
                        str(message.get("subject") or ""),
                        message.get("parentFolderId"),
                        decision.recommended_folder,
                        decision.category,
                        float(decision.confidence),
                        decision.confidence_band,
                        bool(decision.forced_review),
                        list(decision.reasons),
                        list(decision.safety_flags),
                        bool(decision.provider_consulted),
                        provider_cfg.provider if provider_cfg else None,
                        status,
                        error,
                    ),
                )
            connection.commit()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to persist inbox dry-run classification to PostgreSQL.",
        ) from exc


def _load_dry_run_log(account_id: str, limit: int) -> list[dict[str, Any]]:
    if not _database_url():
        return []

    _ensure_account_tables()

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        provider_message_id,
                        account_email,
                        received_at,
                        sender,
                        subject,
                        current_folder,
                        recommended_folder,
                        category,
                        confidence,
                        confidence_band,
                        forced_review,
                        reasons,
                        safety_flags,
                        provider_consulted,
                        provider_name,
                        status,
                        error,
                        created_at
                    FROM inbox_dry_run_classification
                    WHERE account_id = %s
                    ORDER BY COALESCE(received_at, created_at) DESC
                    LIMIT %s
                    """,
                    (account_id, limit),
                )
                rows = cursor.fetchall()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to load inbox dry-run classification log from PostgreSQL.",
        ) from exc

    return [
        {
            "provider_message_id": row[0],
            "account_email": row[1],
            "received_at": row[2].isoformat() if row[2] else None,
            "sender": row[3],
            "subject": row[4],
            "current_folder": row[5],
            "recommended_folder": row[6],
            "category": row[7],
            "confidence": float(row[8]) if row[8] is not None else 0.0,
            "confidence_band": row[9],
            "forced_review": bool(row[10]),
            "reasons": list(row[11] or []),
            "safety_flags": list(row[12] or []),
            "provider_consulted": bool(row[13]),
            "provider": row[14],
            "status": row[15],
            "error": row[16],
            "created_at": row[17].isoformat() if row[17] else None,
        }
        for row in rows
    ]


@app.post("/mail/inbox/classify-dryrun")
async def classify_inbox_dryrun(
    account: str = Query(..., description="Connected account email to scan."),
    limit: int = Query(
        default=INBOX_DRY_RUN_DEFAULT_LIMIT,
        ge=1,
        le=INBOX_DRY_RUN_MAX_LIMIT,
        description="Maximum number of recent inbox messages to classify.",
    ),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    """Fetch a small recent inbox batch and produce dry-run classifications.

    Strictly read-only against Microsoft Graph (GET /me/mailFolders/inbox/
    messages with $top + $select). When Azure OpenAI / Azure AI is
    configured, the classifier asks the provider for a category/confidence
    recommendation, then applies local safety rules. ``10 - Review`` is
    the fallback folder for unclear/sensitive/low-confidence messages.
    """
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)
    target = _scope_account_to_session(rows, account)

    access_token, account_record = await _graph_access_token_for_email(target["email"])
    messages = await _list_inbox_messages(access_token, limit=limit)

    provider_cfg = classifier_module.AzureAIProviderConfig.from_env()

    results: list[dict[str, Any]] = []
    for message in messages:
        try:
            ci = _classification_input_from_message(message)
            decision = await classifier_module.classify_with_provider(
                ci,
                provider_config=provider_cfg,
            )
            _persist_dry_run_classification(
                account_id=account_record["account_id"],
                account_email=account_record["email"],
                message=message,
                decision=decision,
                provider_cfg=provider_cfg,
                status="classified",
            )
            results.append(
                {
                    "provider_message_id": message.get("id"),
                    "received_at": message.get("receivedDateTime"),
                    "sender": _sender_email_from_graph(message),
                    "subject": message.get("subject") or "",
                    "current_folder": message.get("parentFolderId"),
                    "recommendation": decision.to_dict(),
                    "provider_consulted": bool(decision.provider_consulted),
                    "status": "classified",
                }
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("inbox.dryrun.classify_failed message_id=%s", message.get("id"))
            results.append(
                {
                    "provider_message_id": message.get("id"),
                    "received_at": message.get("receivedDateTime"),
                    "sender": _sender_email_from_graph(message),
                    "subject": message.get("subject") or "",
                    "current_folder": message.get("parentFolderId"),
                    "recommendation": None,
                    "provider_consulted": False,
                    "status": "error",
                    "error": str(exc),
                }
            )

    return {
        "dry_run": True,
        "destructive": False,
        "generated_at": _utcnow().isoformat(),
        "account": {
            "email": account_record["email"],
            "display_name": account_record["display_name"],
        },
        "limit": limit,
        "fetched": len(messages),
        "classified": sum(1 for r in results if r["status"] == "classified"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "provider": {
            "selected": provider_cfg.provider,
            "configured": provider_cfg.is_configured(),
            "consulted": any(bool(r.get("provider_consulted")) for r in results),
        },
        "review_folder": classifier_module.REVIEW_FOLDER,
        "results": results,
    }


@app.get("/mail/inbox/classify-dryrun/log")
def classify_inbox_dryrun_log(
    account: str = Query(..., description="Connected account email."),
    limit: int = Query(
        default=INBOX_DRY_RUN_DEFAULT_LIMIT,
        ge=1,
        le=INBOX_DRY_RUN_MAX_LIMIT,
    ),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    """Read-only view of persisted dry-run classifications for an account."""
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)
    target = _scope_account_to_session(rows, account)

    entries = _load_dry_run_log(target["account_id"], limit=limit)
    return {
        "generated_at": _utcnow().isoformat(),
        "account": {
            "email": target["email"],
            "display_name": target["display_name"],
        },
        "limit": limit,
        "count": len(entries),
        "entries": entries,
        "review_folder": classifier_module.REVIEW_FOLDER,
    }


@app.get("/classifier/rules")
def list_classifier_rules(
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    _resolve_session_user_email(linked_email)
    return {"rules": _load_classifier_rules()}


@app.post("/classifier/rules")
async def create_classifier_rule(
    request: Request,
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    user_email = _resolve_session_user_email(linked_email)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}

    match_field = str(body.get("match_field") or "").strip()
    pattern = str(body.get("pattern") or "").strip()
    category = str(body.get("category") or "").strip()
    confidence = float(body.get("confidence") or 0.95)
    reason = str(body.get("reason") or "").strip()

    if match_field not in ("sender", "subject", "body", "any"):
        raise HTTPException(
            status_code=400,
            detail="match_field must be sender, subject, body, or any.",
        )
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern is required.")
    if category not in classifier_module.ALLOWED_CATEGORIES:
        allowed = ", ".join(classifier_module.ALLOWED_CATEGORIES)
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of: {allowed}",
        )
    if not (0.0 < confidence <= 1.0):
        raise HTTPException(status_code=400, detail="confidence must be between 0.0 and 1.0.")

    rule = _create_classifier_rule(
        match_field=match_field,
        pattern=pattern,
        category=category,
        confidence=confidence,
        reason=reason,
        created_by=user_email,
    )
    return rule


@app.delete("/classifier/rules/{rule_id}")
def delete_classifier_rule(
    rule_id: str,
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    _resolve_session_user_email(linked_email)
    deleted = _delete_classifier_rule(rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Rule not found.")
    return {"deleted": True, "id": rule_id}


@app.patch("/classifier/rules/{rule_id}")
async def update_classifier_rule(
    rule_id: str,
    request: Request,
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    _resolve_session_user_email(linked_email)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    if "enabled" not in body:
        raise HTTPException(status_code=400, detail="enabled field is required.")
    updated = _set_classifier_rule_enabled(rule_id, bool(body["enabled"]))
    if not updated:
        raise HTTPException(status_code=404, detail="Rule not found.")
    return {"updated": True, "id": rule_id, "enabled": bool(body["enabled"])}


# =========================================================================
# Approved inbox-move action
# =========================================================================
#
# This is the first slice that ACTUALLY MUTATES the mailbox. It is gated
# by a hard contract:
#
# * Only the connected account's session can request a move (account
#   scoping is enforced exactly like the dry-run endpoints).
# * Each requested message must already have a persisted dry-run row —
#   no "blind moves". The dry-run row provides the recommended folder
#   and the forced_review flag the safety pass enforces.
# * forced_review messages and any message whose recommendation is
#   already 10 - Review go to 10 - Review only — never to a business
#   folder, regardless of operator pressure.
# * The Graph endpoint used is POST /me/messages/{id}/move. There is no
#   delete, no send, no archive. v1 explicitly excludes those.
# * Every attempt persists a mailbox_move_action row (succeeded /
#   failed / skipped) so the activity log can render an honest history.
# * Re-issuing a move for a message that has a recent succeeded row is
#   a no-op (idempotent; we return the existing row's metadata).

INBOX_MOVE_MAX_BATCH = 25
INBOX_AUTOMATION_MAX_BATCH = 25
INBOX_AUTOMATION_DEFAULT_SCAN_LIMIT = 100
INBOX_AUTOMATION_MAX_SCAN_LIMIT = 500
INBOX_AUTOMATION_DEFAULT_MOVE_LIMIT = 25
INBOX_AUTOMATION_MAX_MOVE_LIMIT = 100
INBOX_AUTOMATION_DEFAULT_CONFIDENCE = 0.90
AUTOMATION_ALLOWED_CATEGORIES = {
    "it_reports",
    "newsletters_news",
    "marketing_promotions",
    "notifications_system",
    "finance_money",
    "service_updates",
    "social_media",
    "sales_crm",
}


def _resolve_target_folder_id(account_id: str, recommended_folder: str) -> tuple[str | None, str]:
    """Resolve a folder name to its provider folder id from the persisted inventory.

    Returns ``(provider_folder_id_or_None, canonical_name)``. If the folder
    has not been inventoried yet (typical when the operator forgot to run
    Bootstrap), the caller falls back to ``10 - Review`` for forced-review
    cases, or refuses the move and asks the operator to bootstrap.
    """
    target_name = recommended_folder or classifier_module.REVIEW_FOLDER
    folders = _load_folder_inventory(account_id)
    folded = _fold_name(target_name)
    for folder in folders:
        canonical = folder.get("canonical_name") or folder.get("displayName") or ""
        if _fold_name(canonical) == folded:
            return folder.get("id"), canonical
        if _fold_name(folder.get("displayName") or "") == folded:
            return folder.get("id"), canonical or folder.get("displayName") or target_name
    return None, target_name


def _load_dry_run_row(account_id: str, provider_message_id: str) -> dict[str, Any] | None:
    if not _database_url():
        return None

    _ensure_account_tables()

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        id,
                        provider_message_id,
                        recommended_folder,
                        forced_review,
                        category,
                        confidence,
                        confidence_band,
                        current_folder,
                        safety_flags
                    FROM inbox_dry_run_classification
                    WHERE account_id = %s AND provider_message_id = %s
                    LIMIT 1
                    """,
                    (account_id, provider_message_id),
                )
                row = cursor.fetchone()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to load dry-run classification row from PostgreSQL.",
        ) from exc

    if not row:
        return None
    return {
        "id": row[0],
        "provider_message_id": row[1],
        "recommended_folder": row[2],
        "forced_review": bool(row[3]),
        "category": row[4],
        "confidence": float(row[5]) if row[5] is not None else 0.0,
        "confidence_band": row[6],
        "current_folder": row[7],
        "safety_flags": list(row[8] or []),
    }


def _existing_succeeded_move(account_id: str, provider_message_id: str) -> dict[str, Any] | None:
    if not _database_url():
        return None

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        id,
                        destination_folder_id,
                        destination_folder_name,
                        forced_review,
                        completed_at
                    FROM mailbox_move_action
                    WHERE account_id = %s
                      AND provider_message_id = %s
                      AND status = 'succeeded'
                    ORDER BY requested_at DESC
                    LIMIT 1
                    """,
                    (account_id, provider_message_id),
                )
                row = cursor.fetchone()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to look up prior mailbox_move_action rows.",
        ) from exc

    if not row:
        return None
    return {
        "id": row[0],
        "destination_folder_id": row[1],
        "destination_folder_name": row[2],
        "forced_review": bool(row[3]),
        "completed_at": row[4].isoformat() if row[4] else None,
    }


def _persist_move_action(
    account_id: str,
    account_email: str,
    requested_by_email: str,
    provider_message_id: str,
    source_folder_id: str | None,
    destination_folder_id: str | None,
    destination_folder_name: str,
    dry_run_classification_id: str | None,
    forced_review: bool,
    status: str,
    error: str | None,
    completed: bool,
    categories_applied: list[str] | None = None,
    category_error: str | None = None,
) -> str:
    """Insert a mailbox_move_action row. Returns the new row id."""
    action_id = str(uuid.uuid4())
    if not _database_url():
        return action_id

    psycopg = _psycopg()
    _ensure_account_tables()

    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO mailbox_move_action (
                        id,
                        account_id,
                        account_email,
                        provider_message_id,
                        source_folder_id,
                        destination_folder_id,
                        destination_folder_name,
                        dry_run_classification_id,
                        forced_review,
                        status,
                        error,
                        categories_applied,
                        category_error,
                        requested_by_email,
                        completed_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, CASE WHEN %s THEN now() ELSE NULL END)
                    """,
                    (
                        action_id,
                        account_id,
                        account_email,
                        provider_message_id,
                        source_folder_id,
                        destination_folder_id,
                        destination_folder_name,
                        dry_run_classification_id,
                        forced_review,
                        status,
                        error,
                        json.dumps(categories_applied or []),
                        category_error,
                        requested_by_email,
                        completed,
                    ),
                )
            connection.commit()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to persist mailbox_move_action row to PostgreSQL.",
        ) from exc

    return action_id


async def _graph_move_message(
    access_token: str, provider_message_id: str, destination_folder_id: str
) -> dict[str, Any]:
    """Microsoft Graph: POST /me/messages/{id}/move with destinationId."""
    return await _graph_post(
        access_token,
        f"/me/messages/{provider_message_id}/move",
        {"destinationId": destination_folder_id},
    )


def _parse_json_list(raw_value: Any) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return [str(value) for value in raw_value if value]
    try:
        parsed = json.loads(str(raw_value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(value) for value in parsed if value]


def _load_move_actions(account_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    if not _database_url():
        return []

    _ensure_account_tables()

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                query = """
                    SELECT
                        provider_message_id,
                        account_email,
                        source_folder_id,
                        destination_folder_id,
                        destination_folder_name,
                        forced_review,
                        status,
                        error,
                        categories_applied,
                        category_error,
                        requested_by_email,
                        requested_at,
                        completed_at
                    FROM mailbox_move_action
                    WHERE account_id = %s
                    ORDER BY requested_at DESC
                    """
                if limit is None:
                    cursor.execute(query, (account_id,))
                else:
                    cursor.execute(query + " LIMIT %s", (account_id, limit))
                rows = cursor.fetchall()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to load mailbox_move_action rows from PostgreSQL.",
        ) from exc

    return [
        {
            "provider_message_id": row[0],
            "account_email": row[1],
            "source_folder_id": row[2],
            "destination_folder_id": row[3],
            "destination_folder_name": row[4],
            "forced_review": bool(row[5]),
            "status": row[6],
            "error": row[7],
            "categories_applied": _parse_json_list(row[8]),
            "category_error": row[9],
            "requested_by_email": row[10],
            "requested_at": row[11].isoformat() if row[11] else None,
            "completed_at": row[12].isoformat() if row[12] else None,
        }
        for row in rows
    ]


def _update_move_action_categories(
    account_id: str,
    provider_message_id: str,
    categories_applied: list[str],
    category_error: str | None,
) -> None:
    if not _database_url():
        return

    _ensure_account_tables()
    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE mailbox_move_action
                    SET categories_applied = %s,
                        category_error = %s
                    WHERE account_id = %s
                      AND provider_message_id = %s
                      AND status = 'succeeded'
                    """,
                    (
                        json.dumps(categories_applied or []),
                        category_error,
                        account_id,
                        provider_message_id,
                    ),
                )
            connection.commit()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to update mailbox_move_action category audit fields.",
        ) from exc


@app.post("/mail/inbox/move")
async def move_inbox_messages(
    request: Request,
    account: str = Query(..., description="Connected account email."),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    """Move one or more dry-run-classified messages to the recommended folder.

    Required body shape:

    .. code-block:: json

        {"provider_message_ids": ["AAMk...=", "AAMk...="]}

    Safety contract:

    * The session must be authenticated (``dyc_account_email`` cookie).
    * ``account`` must match an account already linked to the session.
    * Each ``provider_message_id`` must already have a persisted dry-run
      row for this account — otherwise that id is rejected as
      ``no_dry_run_row`` and no Graph call is made.
    * The destination folder is taken from the dry-run row's
      ``recommended_folder``. ``forced_review`` rows (and any rows whose
      recommendation is already ``10 - Review``) move only to
      ``10 - Review`` — never to a business folder, regardless of
      caller pressure.
    * The mailbox folder inventory must contain the destination folder
      (run ``/mail/folders/bootstrap`` first if not). Without it the
      target id cannot be resolved and the per-message attempt is
      recorded as ``status: skipped`` with reason
      ``destination_folder_not_inventoried``.
    * If a recent ``mailbox_move_action`` row already shows ``succeeded``
      for the same ``(account_id, provider_message_id)``, the request is
      idempotent: we return the existing row's destination metadata and
      skip the Graph call.
    * v1 never deletes or sends mail. This endpoint only moves.
    """
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)
    target = _scope_account_to_session(rows, account)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object.")
    raw_ids = body.get("provider_message_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(
            status_code=400,
            detail="provider_message_ids must be a non-empty array of message ids.",
        )
    if len(raw_ids) > INBOX_MOVE_MAX_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"Too many message ids in one request (max {INBOX_MOVE_MAX_BATCH}).",
        )

    provider_message_ids: list[str] = []
    for value in raw_ids:
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(
                status_code=400,
                detail="Each provider_message_id must be a non-empty string.",
            )
        provider_message_ids.append(value.strip())

    access_token, account_record = await _graph_access_token_for_email(target["email"])
    account_id = account_record["account_id"]
    account_email = account_record["email"]

    results: list[dict[str, Any]] = []
    for provider_message_id in provider_message_ids:
        # Idempotency: if a prior succeeded move exists, return that and
        # skip the Graph call.
        existing = _existing_succeeded_move(account_id, provider_message_id)
        if existing:
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "status": "already_moved",
                    "destination_folder_id": existing["destination_folder_id"],
                    "destination_folder_name": existing["destination_folder_name"],
                    "forced_review": existing["forced_review"],
                    "completed_at": existing["completed_at"],
                }
            )
            continue

        dry_run_row = _load_dry_run_row(account_id, provider_message_id)
        if not dry_run_row:
            _persist_move_action(
                account_id=account_id,
                account_email=account_email,
                requested_by_email=user_email,
                provider_message_id=provider_message_id,
                source_folder_id=None,
                destination_folder_id=None,
                destination_folder_name=classifier_module.REVIEW_FOLDER,
                dry_run_classification_id=None,
                forced_review=False,
                status="rejected",
                error="no_dry_run_row",
                completed=False,
            )
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "status": "rejected",
                    "error": "no_dry_run_row",
                    "destination_folder_name": None,
                }
            )
            continue

        # Safety pass: forced_review messages, or messages whose
        # recommendation is already 10 - Review, MUST go to 10 - Review.
        # Anything else is allowed to follow the recommendation.
        recommended = dry_run_row["recommended_folder"] or classifier_module.REVIEW_FOLDER
        forced = bool(dry_run_row["forced_review"])
        if forced or recommended == classifier_module.REVIEW_FOLDER:
            target_name = classifier_module.REVIEW_FOLDER
            forced = True
        else:
            target_name = recommended

        destination_folder_id, resolved_name = _resolve_target_folder_id(account_id, target_name)
        if not destination_folder_id:
            _persist_move_action(
                account_id=account_id,
                account_email=account_email,
                requested_by_email=user_email,
                provider_message_id=provider_message_id,
                source_folder_id=dry_run_row.get("current_folder"),
                destination_folder_id=None,
                destination_folder_name=target_name,
                dry_run_classification_id=dry_run_row["id"],
                forced_review=forced,
                status="skipped",
                error="destination_folder_not_inventoried",
                completed=False,
            )
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "status": "skipped",
                    "error": "destination_folder_not_inventoried",
                    "destination_folder_name": target_name,
                    "forced_review": forced,
                }
            )
            continue

        try:
            await _graph_move_message(access_token, provider_message_id, destination_folder_id)
        except HTTPException as exc:
            detail = exc.detail
            error_message = detail.get("message") if isinstance(detail, dict) else str(detail)
            _persist_move_action(
                account_id=account_id,
                account_email=account_email,
                requested_by_email=user_email,
                provider_message_id=provider_message_id,
                source_folder_id=dry_run_row.get("current_folder"),
                destination_folder_id=destination_folder_id,
                destination_folder_name=resolved_name or target_name,
                dry_run_classification_id=dry_run_row["id"],
                forced_review=forced,
                status="failed",
                error=error_message or "graph_move_failed",
                completed=False,
            )
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "status": "failed",
                    "error": error_message or "graph_move_failed",
                    "destination_folder_id": destination_folder_id,
                    "destination_folder_name": resolved_name or target_name,
                    "forced_review": forced,
                }
            )
            continue

        applied_categories: list[str] = []
        label_error: str | None = None
        if _outlook_category_labels_enabled():
            try:
                fetched = await _graph_get(
                    access_token,
                    f"/me/messages/{provider_message_id}",
                    params={"$select": "id,categories,subject,bodyPreview"},
                )
                reconstructed_decision = classifier_module.ClassificationDecision(
                    category=dry_run_row.get("category") or "unknown_ambiguous",
                    recommended_folder=target_name,
                    confidence=float(dry_run_row.get("confidence") or 0.0),
                    confidence_band=dry_run_row.get("confidence_band") or "low",
                    reasons=(),
                    safety_flags=tuple(dry_run_row.get("safety_flags") or []),
                    forced_review=forced,
                )
                desired_categories = _desired_attention_categories(
                    reconstructed_decision, fetched, moved=True
                )
                applied_categories = await _apply_message_categories(
                    access_token,
                    provider_message_id,
                    _message_categories(fetched),
                    desired_categories,
                )
            except Exception as exc:
                label_error = _category_apply_error(exc)

        _persist_move_action(
            account_id=account_id,
            account_email=account_email,
            requested_by_email=user_email,
            provider_message_id=provider_message_id,
            source_folder_id=dry_run_row.get("current_folder"),
            destination_folder_id=destination_folder_id,
            destination_folder_name=resolved_name or target_name,
            dry_run_classification_id=dry_run_row["id"],
            forced_review=forced,
            status="succeeded",
            error=None,
            completed=True,
            categories_applied=applied_categories,
            category_error=label_error,
        )
        results.append(
            {
                "provider_message_id": provider_message_id,
                "status": "succeeded",
                "destination_folder_id": destination_folder_id,
                "destination_folder_name": resolved_name or target_name,
                "forced_review": forced,
                "categories_applied": applied_categories,
                "category_error": label_error,
            }
        )

    return {
        "account": {
            "email": account_email,
            "display_name": account_record["display_name"],
        },
        "requested": len(provider_message_ids),
        "succeeded": sum(1 for r in results if r["status"] == "succeeded"),
        "already_moved": sum(1 for r in results if r["status"] == "already_moved"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "rejected": sum(1 for r in results if r["status"] == "rejected"),
        "review_folder": classifier_module.REVIEW_FOLDER,
        "results": results,
    }


def _automation_safety_skip_reason(
    decision: classifier_module.ClassificationDecision,
    min_confidence: float,
) -> str | None:
    if decision.forced_review:
        return "forced_review"
    if decision.recommended_folder == classifier_module.REVIEW_FOLDER:
        return "review_folder"
    if decision.category not in AUTOMATION_ALLOWED_CATEGORIES:
        return "category_not_automation_allowed"
    if decision.confidence < min_confidence:
        return "confidence_below_automation_threshold"
    if decision.confidence_band != "high":
        return "confidence_band_not_high"
    if decision.safety_flags:
        return "safety_flags_present"
    return None


_DYC_LABEL_NAMES: frozenset[str] = frozenset(
    spec["displayName"] for spec in DEFAULT_OUTLOOK_CATEGORY_SPECS
)


def _message_categories(message: dict[str, Any]) -> list[str]:
    categories = message.get("categories")
    if not isinstance(categories, list):
        return []
    return [str(category) for category in categories if category]


def _has_any_dyc_label(categories: list[str]) -> bool:
    return any(c in _DYC_LABEL_NAMES for c in categories)


def _desired_attention_categories(
    decision: classifier_module.ClassificationDecision,
    message: dict[str, Any],
    *,
    moved: bool,
) -> list[str]:
    labels: list[str] = []
    subject = str(message.get("subject") or "").lower()
    body = str(message.get("bodyPreview") or "").lower()
    combined = f"{subject}\n{body}"

    if decision.forced_review or decision.recommended_folder == classifier_module.REVIEW_FOLDER:
        labels.append("< Review >")

    if decision.category == "human_direct":
        labels.append("< Reply >")
    if decision.category == "finance_money":
        labels.append("< Money >")
    if decision.category == "legal_contracts" or "legal_or_contractual" in decision.safety_flags:
        labels.append("< Legal >")
    if "sensitive_content" in decision.safety_flags:
        labels.append("< Customer >")
    if decision.category in {"newsletters_news", "marketing_promotions"}:
        labels.append("< Read Later >")
    if decision.category in {"it_reports", "notifications_system", "service_updates"}:
        labels.append("< FYI >")
    if decision.category == "meetings_scheduling" or any(
        word in combined
        for word in ("flight", "hotel", "boarding", "reservation", "travel", "itinerary")
    ):
        labels.append("< Travel >")
    if any(word in combined for word in ("urgent", "today", "due today", "asap")):
        labels.append("< Today >")
    elif any(word in combined for word in ("this week", "by friday", "deadline")):
        labels.append("< This Week >")
    if any(phrase in combined for phrase in _INVOICE_PHRASES):
        labels.append("< Pay This >")

    return list(dict.fromkeys(labels))


def _folder_attention_categories(destination_folder_name: str | None) -> list[str]:
    folder = (destination_folder_name or "").strip()
    if folder == classifier_module.REVIEW_FOLDER:
        return ["< Review >"]
    if folder in {"20 - News", "50 - Marketing", "54 - LinkedIn"}:
        return ["< Read Later >"]
    if folder == "30 - Money":
        return ["< Money >"]
    if folder in {"40 - Notifications", "56 - Pipedrive", "90 - IT Reports"}:
        return ["< FYI >"]
    if folder == "70 - Contracts":
        return ["< Legal >"]
    if folder == "80 - Travel":
        return ["< Travel >"]
    return []


async def _apply_message_categories(
    access_token: str,
    provider_message_id: str,
    existing_categories: list[str],
    desired_categories: list[str],
) -> list[str]:
    if not desired_categories:
        return existing_categories
    merged = list(dict.fromkeys([*existing_categories, *desired_categories]))
    if merged == existing_categories:
        return existing_categories
    await _graph_patch(
        access_token,
        f"/me/messages/{provider_message_id}",
        {"categories": merged},
    )
    return merged


def _category_apply_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        return detail.get("message") if isinstance(detail, dict) else str(detail)
    return str(exc)


def _outlook_category_labels_enabled() -> bool:
    return _bool_env("OUTLOOK_CATEGORY_LABELS_ENABLED")


def _sms_alerts_enabled() -> bool:
    return bool(
        os.getenv("ACS_CONNECTION_STRING")
        and os.getenv("ACS_FROM_NUMBER")
        and os.getenv("ALERT_PHONE_NUMBER")
    )


def _is_pay_this_message(message: dict[str, Any]) -> bool:
    subject = str(message.get("subject") or "").lower()
    body = str(message.get("bodyPreview") or "").lower()
    combined = f"{subject}\n{body}"
    return any(phrase in combined for phrase in _INVOICE_PHRASES)


def _is_sms_urgent(
    decision: classifier_module.ClassificationDecision,
    message: dict[str, Any],
) -> bool:
    if decision.category != "human_direct" or decision.confidence < 0.85:
        return False
    subject = str(message.get("subject") or "").lower()
    body = str(message.get("bodyPreview") or "").lower()
    combined = f"{subject}\n{body}"
    return any(phrase in combined for phrase in _URGENT_PHRASES)


def _sms_already_sent(account_id: str, provider_message_id: str) -> bool:
    if not _database_url():
        return False
    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT 1 FROM mailbox_move_action
                    WHERE account_id = %s
                      AND provider_message_id = %s
                      AND sms_notified_at IS NOT NULL
                    LIMIT 1
                    """,
                    (account_id, provider_message_id),
                )
                return cursor.fetchone() is not None
    except psycopg.Error:
        return False


def _record_sms_sent(account_id: str, provider_message_id: str) -> None:
    if not _database_url():
        return
    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE mailbox_move_action
                    SET sms_notified_at = now()
                    WHERE account_id = %s
                      AND provider_message_id = %s
                      AND status = 'succeeded'
                    """,
                    (account_id, provider_message_id),
                )
            connection.commit()
    except psycopg.Error:
        pass


def _send_sms_alert(subject: str, sender_email: str, reason: str) -> None:
    from azure.communication.sms import SmsClient

    connection_string = os.getenv("ACS_CONNECTION_STRING", "")
    from_number = os.getenv("ACS_FROM_NUMBER", "")
    to_number = os.getenv("ALERT_PHONE_NUMBER", "")
    if reason == "pay_this":
        body = f"Invoice to pay: {subject} (from {sender_email})"
    else:
        body = f"Urgent email: {subject} (from {sender_email})"
    client = SmsClient.from_connection_string(connection_string)
    client.send(from_=from_number, to=[to_number], message=body)


async def _backfill_recent_move_labels(
    access_token: str,
    account_id: str,
    limit: int = 50,
) -> dict[str, Any]:
    if not _outlook_category_labels_enabled():
        return {"enabled": False, "checked": 0, "updated": 0, "skipped": 0, "failed": 0}

    checked = 0
    updated = 0
    skipped = 0
    failed = 0
    results: list[dict[str, Any]] = []
    for action in _load_move_actions(account_id, limit=limit):
        if action.get("status") != "succeeded":
            continue
        desired_categories = _folder_attention_categories(action.get("destination_folder_name"))
        if not desired_categories:
            continue

        provider_message_id = action["provider_message_id"]
        checked += 1
        try:
            message = await _graph_get(
                access_token,
                f"/me/messages/{provider_message_id}",
                params={"$select": "id,categories"},
            )
            actual_categories = _message_categories(message)
            if _has_any_dyc_label(actual_categories):
                skipped += 1
                continue

            applied_categories = await _apply_message_categories(
                access_token,
                provider_message_id,
                actual_categories,
                desired_categories,
            )
            _update_move_action_categories(
                account_id,
                provider_message_id,
                applied_categories,
                None,
            )
            updated += 1
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "status": "updated",
                    "categories_applied": applied_categories,
                }
            )
        except Exception as exc:
            failed += 1
            error = _category_apply_error(exc)
            _update_move_action_categories(
                account_id,
                provider_message_id,
                action.get("categories_applied") or [],
                error,
            )
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "status": "failed",
                    "error": error,
                }
            )

    return {
        "enabled": True,
        "checked": checked,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


@app.post("/mail/inbox/automove")
async def automove_inbox_messages(
    account: str = Query(..., description="Connected account email."),
    limit: int = Query(
        default=INBOX_AUTOMATION_DEFAULT_SCAN_LIMIT,
        ge=1,
        le=INBOX_AUTOMATION_MAX_SCAN_LIMIT,
        description="Maximum recent Inbox messages to scan.",
    ),
    move_limit: int = Query(
        default=INBOX_AUTOMATION_DEFAULT_MOVE_LIMIT,
        ge=1,
        le=INBOX_AUTOMATION_MAX_MOVE_LIMIT,
        description="Maximum messages this run may move.",
    ),
    min_confidence: float = Query(
        default=INBOX_AUTOMATION_DEFAULT_CONFIDENCE,
        ge=0.90,
        le=1.0,
        description="Minimum classifier confidence required for automated moves.",
    ),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    """Classify recent inbox messages and move only automation-safe matches.

    This endpoint is intentionally conservative. It never deletes, sends, or
    archives mail. It moves only high-confidence, non-review recommendations
    in explicitly automation-allowed categories. All skipped and moved
    decisions are logged in ``mailbox_move_action`` for the Audit Log.
    """
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)
    target = _scope_account_to_session(rows, account)
    return await _automove_for_account(
        requested_by_email=user_email,
        target=target,
        scan_limit=limit,
        move_limit=move_limit,
        min_confidence=min_confidence,
    )


async def _automove_for_account(
    requested_by_email: str,
    target: dict[str, Any],
    scan_limit: int,
    move_limit: int,
    min_confidence: float,
) -> dict[str, Any]:
    health = _automation_health_for_account(target)
    if not health["automation_ready"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Automation is not ready for this account.",
                "automation_health": health,
            },
        )

    access_token, account_record = await _graph_access_token_for_email(target["email"])
    account_id = account_record["account_id"]
    account_email = account_record["email"]

    label_bootstrap_error = None
    if _outlook_category_labels_enabled():
        try:
            await _ensure_default_outlook_categories(access_token)
        except Exception as exc:
            label_bootstrap_error = f"category_bootstrap_failed: {_category_apply_error(exc)}"

    label_backfill = {
        "enabled": _outlook_category_labels_enabled(),
        "checked": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
    }
    if _outlook_category_labels_enabled() and not label_bootstrap_error:
        try:
            label_backfill = await _backfill_recent_move_labels(
                access_token,
                account_id,
                limit=move_limit,
            )
        except Exception as exc:
            label_backfill = {
                "enabled": True,
                "checked": 0,
                "updated": 0,
                "skipped": 0,
                "failed": 1,
                "error": _category_apply_error(exc),
            }

    messages = await _list_inbox_messages_paginated(access_token, scan_limit=scan_limit)
    provider_cfg = classifier_module.AzureAIProviderConfig.from_env()

    results: list[dict[str, Any]] = []
    moved_count = 0
    for message in messages:
        provider_message_id = str(message.get("id") or "")
        if not provider_message_id:
            continue
        try:
            ci = _classification_input_from_message(message)
            decision = await classifier_module.classify_with_provider(
                ci,
                provider_config=provider_cfg,
            )
            _persist_dry_run_classification(
                account_id=account_id,
                account_email=account_email,
                message=message,
                decision=decision,
                provider_cfg=provider_cfg,
                status="classified",
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("inbox.automove.classify_failed message_id=%s", provider_message_id)
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "status": "failed",
                    "error": f"classification_failed: {exc}",
                    "moved": False,
                }
            )
            continue

        dry_run_row = _load_dry_run_row(account_id, provider_message_id)
        skip_reason = _automation_safety_skip_reason(decision, min_confidence)
        if skip_reason:
            _persist_move_action(
                account_id=account_id,
                account_email=account_email,
                requested_by_email=requested_by_email,
                provider_message_id=provider_message_id,
                source_folder_id=message.get("parentFolderId"),
                destination_folder_id=None,
                destination_folder_name=decision.recommended_folder,
                dry_run_classification_id=dry_run_row["id"] if dry_run_row else None,
                forced_review=bool(decision.forced_review),
                status="skipped",
                error=f"automation_{skip_reason}",
                completed=False,
            )
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "subject": message.get("subject") or "",
                    "status": "skipped",
                    "error": f"automation_{skip_reason}",
                    "recommended_folder": decision.recommended_folder,
                    "confidence": decision.confidence,
                    "moved": False,
                }
            )
            continue

        if moved_count >= move_limit:
            _persist_move_action(
                account_id=account_id,
                account_email=account_email,
                requested_by_email=requested_by_email,
                provider_message_id=provider_message_id,
                source_folder_id=message.get("parentFolderId"),
                destination_folder_id=None,
                destination_folder_name=decision.recommended_folder,
                dry_run_classification_id=dry_run_row["id"] if dry_run_row else None,
                forced_review=False,
                status="skipped",
                error="automation_move_limit_reached",
                completed=False,
            )
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "subject": message.get("subject") or "",
                    "status": "skipped",
                    "error": "automation_move_limit_reached",
                    "recommended_folder": decision.recommended_folder,
                    "confidence": decision.confidence,
                    "moved": False,
                }
            )
            continue

        existing = _existing_succeeded_move(account_id, provider_message_id)
        if existing:
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "subject": message.get("subject") or "",
                    "status": "already_moved",
                    "destination_folder_name": existing["destination_folder_name"],
                    "moved": False,
                }
            )
            continue

        destination_folder_id, resolved_name = _resolve_target_folder_id(
            account_id, decision.recommended_folder
        )
        if not destination_folder_id:
            _persist_move_action(
                account_id=account_id,
                account_email=account_email,
                requested_by_email=requested_by_email,
                provider_message_id=provider_message_id,
                source_folder_id=message.get("parentFolderId"),
                destination_folder_id=None,
                destination_folder_name=decision.recommended_folder,
                dry_run_classification_id=dry_run_row["id"] if dry_run_row else None,
                forced_review=False,
                status="skipped",
                error="automation_destination_folder_not_inventoried",
                completed=False,
            )
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "subject": message.get("subject") or "",
                    "status": "skipped",
                    "error": "automation_destination_folder_not_inventoried",
                    "recommended_folder": decision.recommended_folder,
                    "moved": False,
                }
            )
            continue

        applied_categories = []
        label_error = None
        if _outlook_category_labels_enabled():
            desired_categories = _desired_attention_categories(
                decision,
                message,
                moved=True,
            )
            if label_bootstrap_error:
                label_error = label_bootstrap_error
            else:
                try:
                    applied_categories = await _apply_message_categories(
                        access_token,
                        provider_message_id,
                        _message_categories(message),
                        desired_categories,
                    )
                except Exception as exc:
                    label_error = _category_apply_error(exc)

        try:
            await _graph_move_message(access_token, provider_message_id, destination_folder_id)
        except HTTPException as exc:
            detail = exc.detail
            error_message = detail.get("message") if isinstance(detail, dict) else str(detail)
            _persist_move_action(
                account_id=account_id,
                account_email=account_email,
                requested_by_email=requested_by_email,
                provider_message_id=provider_message_id,
                source_folder_id=message.get("parentFolderId"),
                destination_folder_id=destination_folder_id,
                destination_folder_name=resolved_name or decision.recommended_folder,
                dry_run_classification_id=dry_run_row["id"] if dry_run_row else None,
                forced_review=False,
                status="failed",
                error=error_message or "automation_graph_move_failed",
                completed=False,
            )
            results.append(
                {
                    "provider_message_id": provider_message_id,
                    "subject": message.get("subject") or "",
                    "status": "failed",
                    "error": error_message or "automation_graph_move_failed",
                    "destination_folder_name": resolved_name or decision.recommended_folder,
                    "moved": False,
                }
            )
            continue

        _persist_move_action(
            account_id=account_id,
            account_email=account_email,
            requested_by_email=requested_by_email,
            provider_message_id=provider_message_id,
            source_folder_id=message.get("parentFolderId"),
            destination_folder_id=destination_folder_id,
            destination_folder_name=resolved_name or decision.recommended_folder,
            dry_run_classification_id=dry_run_row["id"] if dry_run_row else None,
            forced_review=False,
            status="succeeded",
            error=None,
            completed=True,
            categories_applied=applied_categories,
            category_error=label_error,
        )
        moved_count += 1

        sms_reason: str | None = None
        if _sms_alerts_enabled() and not _sms_already_sent(account_id, provider_message_id):
            if "< Pay This >" in applied_categories:
                sms_reason = "pay_this"
            elif _is_sms_urgent(decision, message):
                sms_reason = "urgent"
        if sms_reason:
            try:
                await asyncio.to_thread(
                    _send_sms_alert,
                    message.get("subject") or "",
                    _sender_email_from_graph(message),
                    sms_reason,
                )
                _record_sms_sent(account_id, provider_message_id)
            except Exception:
                logger.exception("sms.alert.failed message_id=%s", provider_message_id)

        results.append(
            {
                "provider_message_id": provider_message_id,
                "subject": message.get("subject") or "",
                "status": "succeeded",
                "destination_folder_id": destination_folder_id,
                "destination_folder_name": resolved_name or decision.recommended_folder,
                "confidence": decision.confidence,
                "moved": True,
                "categories_applied": applied_categories,
                "category_error": label_error,
                "sms_sent": sms_reason is not None,
            }
        )

    return {
        "automation": True,
        "destructive": True,
        "operation": "move",
        "generated_at": _utcnow().isoformat(),
        "account": {
            "email": account_email,
            "display_name": account_record["display_name"],
        },
        "limit": scan_limit,
        "scan_limit": scan_limit,
        "move_limit": move_limit,
        "min_confidence": min_confidence,
        "fetched": len(messages),
        "moved": sum(1 for r in results if r["status"] == "succeeded"),
        "already_moved": sum(1 for r in results if r["status"] == "already_moved"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "label_backfill": label_backfill,
        "review_folder": classifier_module.REVIEW_FOLDER,
        "results": results,
    }


def _require_automation_run_token(request: Request) -> None:
    expected = os.getenv("AUTOMATION_RUN_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="AUTOMATION_RUN_TOKEN is not configured.",
        )
    authorization = request.headers.get("authorization") or ""
    bearer = authorization.removeprefix("Bearer ").strip()
    header_token = request.headers.get("x-automation-token") or ""
    provided = bearer or header_token.strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid automation token.")


@app.post("/automation/run")
async def run_scheduled_automation(
    request: Request,
    limit: int = Query(
        default=INBOX_AUTOMATION_DEFAULT_SCAN_LIMIT,
        ge=1,
        le=INBOX_AUTOMATION_MAX_SCAN_LIMIT,
    ),
    move_limit: int = Query(
        default=INBOX_AUTOMATION_DEFAULT_MOVE_LIMIT,
        ge=1,
        le=INBOX_AUTOMATION_MAX_MOVE_LIMIT,
    ),
    min_confidence: float = Query(
        default=INBOX_AUTOMATION_DEFAULT_CONFIDENCE,
        ge=0.90,
        le=1.0,
    ),
) -> dict[str, Any]:
    """Machine endpoint called by the scheduled workflow.

    Runs the same guarded inbox automation path as the UI button, across all
    connected allow-listed accounts. The endpoint is token-protected and does
    not depend on a browser session cookie.
    """
    _require_automation_run_token(request)
    accounts = _list_automation_accounts()

    account_results: list[dict[str, Any]] = []
    for account_row in accounts:
        health = _automation_health_for_account(account_row)
        if health["state"] == "red":
            account_results.append(
                {
                    "account": {"email": account_row["email"]},
                    "status": "skipped",
                    "automation_health": health,
                    "moved": 0,
                    "skipped": 0,
                    "failed": 0,
                }
            )
            continue
        try:
            result = await _automove_for_account(
                requested_by_email="automation@scheduler",
                target=account_row,
                scan_limit=limit,
                move_limit=move_limit,
                min_confidence=min_confidence,
            )
            result["status"] = "completed"
            result["automation_health"] = health
            account_results.append(result)
        except HTTPException as exc:
            detail = exc.detail
            account_results.append(
                {
                    "account": {"email": account_row["email"]},
                    "status": "failed",
                    "automation_health": health,
                    "error": detail,
                    "moved": 0,
                    "skipped": 0,
                    "failed": 1,
                }
            )

    return {
        "automation": True,
        "scheduled": True,
        "generated_at": _utcnow().isoformat(),
        "accounts_seen": len(accounts),
        "accounts_completed": sum(1 for r in account_results if r["status"] == "completed"),
        "accounts_skipped": sum(1 for r in account_results if r["status"] == "skipped"),
        "accounts_failed": sum(1 for r in account_results if r["status"] == "failed"),
        "moved": sum(int(r.get("moved") or 0) for r in account_results),
        "message_skipped": sum(int(r.get("skipped") or 0) for r in account_results),
        "message_failed": sum(int(r.get("failed") or 0) for r in account_results),
        "results": account_results,
    }


@app.post("/automation/backfill-labels")
async def backfill_all_labels(request: Request) -> dict[str, Any]:
    """Retroactively apply Outlook labels to every previously moved message.

    Sweeps the full mailbox_move_action history (no recency limit) for all
    connected accounts. For each succeeded move, fetches the actual message
    from Graph and applies the folder-appropriate label if no DYC label is
    present. Safe to run multiple times — messages that already have a DYC
    label are skipped.
    """
    _require_automation_run_token(request)

    if not _outlook_category_labels_enabled():
        return {
            "enabled": False,
            "generated_at": _utcnow().isoformat(),
            "accounts": [],
        }

    accounts = _list_automation_accounts()
    account_results: list[dict[str, Any]] = []

    for account_row in accounts:
        account_email = account_row["email"]
        try:
            access_token, account_record = await _graph_access_token_for_email(account_email)
            account_id = account_record["account_id"]

            checked = 0
            updated = 0
            skipped = 0
            failed = 0

            for action in _load_move_actions(account_id, limit=None):
                if action.get("status") != "succeeded":
                    continue
                desired_categories = _folder_attention_categories(
                    action.get("destination_folder_name")
                )
                if not desired_categories:
                    continue

                provider_message_id = action["provider_message_id"]
                checked += 1
                try:
                    message = await _graph_get(
                        access_token,
                        f"/me/messages/{provider_message_id}",
                        params={"$select": "id,categories"},
                    )
                    actual_categories = _message_categories(message)
                    if _has_any_dyc_label(actual_categories):
                        skipped += 1
                        continue
                    applied = await _apply_message_categories(
                        access_token,
                        provider_message_id,
                        actual_categories,
                        desired_categories,
                    )
                    _update_move_action_categories(account_id, provider_message_id, applied, None)
                    updated += 1
                except Exception as exc:
                    failed += 1
                    _update_move_action_categories(
                        account_id,
                        provider_message_id,
                        action.get("categories_applied") or [],
                        _category_apply_error(exc),
                    )

            account_results.append(
                {
                    "account": account_email,
                    "status": "completed",
                    "checked": checked,
                    "updated": updated,
                    "skipped": skipped,
                    "failed": failed,
                }
            )
        except Exception as exc:
            account_results.append(
                {
                    "account": account_email,
                    "status": "failed",
                    "error": str(exc),
                }
            )

    return {
        "enabled": True,
        "generated_at": _utcnow().isoformat(),
        "accounts": account_results,
        "total_checked": sum(int(r.get("checked") or 0) for r in account_results),
        "total_updated": sum(int(r.get("updated") or 0) for r in account_results),
        "total_skipped": sum(int(r.get("skipped") or 0) for r in account_results),
        "total_failed": sum(int(r.get("failed") or 0) for r in account_results),
    }


@app.get("/accounts/{email}/dashboard")
def account_dashboard(
    email: str,
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)

    target = next((row for row in rows if row["email"].lower() == email.lower()), None)
    if not target:
        raise HTTPException(
            status_code=404,
            detail="No connected account with that email is linked to this session.",
        )

    return {
        "generated_at": _utcnow().isoformat(),
        "user": {"email": user_email},
        **_account_dashboard_payload(target),
        "pending_instrumentation": [
            {"metric": "email_volume", "reason": PENDING_REASON_INGESTION},
            {"metric": "action_activity", "reason": PENDING_REASON_ACTIONS},
        ],
    }
