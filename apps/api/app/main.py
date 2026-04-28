import base64
import hashlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import Cookie, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

AUTH_COOKIE = "dyc_auth_state"
PKCE_COOKIE = "dyc_auth_pkce"
EMAIL_COOKIE = "dyc_account_email"
NAME_COOKIE = "dyc_account_name"
MICROSOFT_SCOPE = "openid profile email offline_access User.Read Mail.Read Mail.ReadWrite"
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
    {"name": "30 - LinkedIn", "aliases": ("LinkedIn",)},
    {"name": "40 - Notifications", "aliases": ("Notifications",)},
    {"name": "50 - Marketing", "aliases": ("Marketing",)},
    {"name": "60 - Notes", "aliases": ("Notes",)},
    {"name": "70 - Contracts", "aliases": ("Contracts",)},
    {"name": "80 - Travel", "aliases": ("Travel",)},
    {"name": "90 - IT Reports", "aliases": ("IT Reports",)},
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
)


def _bool_env(name: str) -> bool:
    value = os.getenv(name, "")
    return value.lower() in {"1", "true", "yes", "on"}


def _split_csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    if not raw:
        return []
    return [value.strip() for value in raw.split(",") if value.strip()]


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


def _authorize_url(state: str, code_challenge: str, login_hint: str | None = None) -> str:
    tenant_id = _require_env("MICROSOFT_ENTRA_TENANT_ID")
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
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?"
        f"{urlencode(params)}"
    )


def _web_redirect(status: str, **params: str) -> str:
    query_items = {"auth": status}
    query_items.update(params)
    return f"{settings.web_app_url}/?{urlencode(query_items)}"


async def _exchange_code(code: str, verifier: str) -> dict[str, Any]:
    tenant_id = _require_env("MICROSOFT_ENTRA_TENANT_ID")
    client_id = _require_env("MICROSOFT_ENTRA_CLIENT_ID")
    client_secret = _require_env("MICROSOFT_ENTRA_CLIENT_SECRET")
    redirect_uri = _require_env("MICROSOFT_ENTRA_REDIRECT_URI")
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
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


def _session_payload(
    linked_account: dict[str, str] | None = None,
) -> dict[str, Any]:
    variables: dict[str, dict[str, bool]] = {
        name: {"present": bool(os.getenv(name)), "is_secret": is_secret}
        for name, is_secret in _RUNTIME_VARIABLES
    }
    return {
        "environment": settings.app_env,
        "web_app_url": settings.web_app_url,
        "api_base_url": settings.api_base_url,
        "variables": variables,
        "all_required_present": all(v["present"] for v in variables.values()),
        "has_database_url": variables["DATABASE_URL"]["present"],
        "has_entra_client_id": variables["MICROSOFT_ENTRA_CLIENT_ID"]["present"],
        "has_entra_tenant_id": variables["MICROSOFT_ENTRA_TENANT_ID"]["present"],
        "has_entra_client_secret": variables["MICROSOFT_ENTRA_CLIENT_SECRET"]["present"],
        "has_entra_redirect_uri": variables["MICROSOFT_ENTRA_REDIRECT_URI"]["present"],
        "key_vault_refs_enabled": settings.key_vault_refs_enabled,
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
            # sync_event records a single invocation of a sync/bootstrap op so
            # the dashboard can show "last sync at", recent errors, and a feed
            # of real activity events. Operations covered today:
            #   folder.bootstrap, folder.inventory.sync, messages.sync.
            # Counts are operation-specific; columns are kept as nullable
            # integers so future ops can opt in without a migration.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_event (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES connected_account(id)
                        ON DELETE CASCADE,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    folders_seen INTEGER,
                    messages_seen INTEGER,
                    messages_persisted INTEGER,
                    messages_moved INTEGER,
                    errors INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sync_event_account_time
                ON sync_event(account_id, completed_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sync_event_operation_time
                ON sync_event(operation, completed_at DESC)
                """
            )
            # message_sighting records a per-message observation made by the
            # Graph message-sync endpoint. We persist metadata only — never
            # the body. Subject is truncated to 120 chars to limit storage and
            # exposure of sensitive content; see docs/dashboard-instrumentation.md.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS message_sighting (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES connected_account(id)
                        ON DELETE CASCADE,
                    provider_message_id TEXT NOT NULL,
                    folder_provider_id TEXT,
                    folder_display_name TEXT,
                    subject_preview TEXT,
                    received_at TIMESTAMPTZ,
                    is_unread BOOLEAN,
                    has_attachments BOOLEAN,
                    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(account_id, provider_message_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_message_sighting_account_received
                ON message_sighting(account_id, received_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_message_sighting_account_seen
                ON message_sighting(account_id, first_seen_at DESC)
                """
            )
            # mailbox_action_event is the instrumentation seam for the not-yet
            # built move worker. It is created so that the dashboard "moved"
            # counters can read from a stable table; today the table stays
            # empty because nothing writes to it. Do not start populating
            # this table from speculative paths — only the real action worker
            # should write here.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS mailbox_action_event (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES connected_account(id)
                        ON DELETE CASCADE,
                    provider_message_id TEXT,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_folder TEXT,
                    target_folder TEXT,
                    error_message TEXT,
                    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mailbox_action_event_account_time
                ON mailbox_action_event(account_id, occurred_at DESC)
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
    tenant_id = _require_env("MICROSOFT_ENTRA_TENANT_ID")
    client_id = _require_env("MICROSOFT_ENTRA_CLIENT_ID")
    client_secret = _require_env("MICROSOFT_ENTRA_CLIENT_SECRET")
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
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


SUBJECT_PREVIEW_MAX_CHARS = 120

# Cap how many messages /mail/messages/sync pulls per Graph call. Keep it
# bounded so a single sync stays well under the API's request budget; the
# endpoint can be re-run for backfill.
MESSAGE_SYNC_DEFAULT_LIMIT = 50
MESSAGE_SYNC_MAX_LIMIT = 200


def _truncate_subject(subject: str | None) -> str | None:
    if subject is None:
        return None
    cleaned = str(subject).strip()
    if not cleaned:
        return ""
    if len(cleaned) <= SUBJECT_PREVIEW_MAX_CHARS:
        return cleaned
    return cleaned[: SUBJECT_PREVIEW_MAX_CHARS - 1].rstrip() + "…"


def _parse_graph_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Graph uses ISO-8601 with a "Z" suffix for UTC.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _record_sync_event(
    account_id: str,
    operation: str,
    *,
    status: str,
    started_at: datetime,
    folders_seen: int | None = None,
    messages_seen: int | None = None,
    messages_persisted: int | None = None,
    messages_moved: int | None = None,
    errors: int = 0,
    error_message: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Persist a sync_event row. No-op when DATABASE_URL is unset.

    Failures to record are swallowed: instrumentation must never break the
    operation it is observing.
    """
    if not _database_url():
        return
    try:
        _ensure_account_tables()
    except Exception:
        return

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO sync_event (
                        id,
                        account_id,
                        operation,
                        status,
                        folders_seen,
                        messages_seen,
                        messages_persisted,
                        messages_moved,
                        errors,
                        error_message,
                        detail,
                        started_at,
                        completed_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, now()
                    )
                    """,
                    (
                        str(uuid.uuid4()),
                        account_id,
                        operation,
                        status,
                        folders_seen,
                        messages_seen,
                        messages_persisted,
                        messages_moved,
                        errors,
                        error_message,
                        _json_dumps(detail or {}),
                        started_at,
                    ),
                )
            connection.commit()
    except psycopg.Error:
        return


def _json_dumps(value: Any) -> str:
    def _default(obj: Any) -> str:
        if isinstance(obj, datetime):
            return obj.isoformat()
        return str(obj)

    return json.dumps(value, default=_default)


def _persist_message_sightings(
    account_id: str, messages: list[dict[str, Any]]
) -> tuple[int, int]:
    """Persist message metadata. Returns (seen, persisted)."""
    if not messages:
        return (0, 0)
    if not _database_url():
        return (len(messages), 0)

    _ensure_account_tables()
    psycopg = _psycopg()
    persisted = 0
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                for message in messages:
                    provider_message_id = message.get("id")
                    if not provider_message_id:
                        continue
                    received_at = _parse_graph_datetime(message.get("receivedDateTime"))
                    cursor.execute(
                        """
                        INSERT INTO message_sighting (
                            id,
                            account_id,
                            provider_message_id,
                            folder_provider_id,
                            folder_display_name,
                            subject_preview,
                            received_at,
                            is_unread,
                            has_attachments,
                            first_seen_at,
                            last_seen_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                        ON CONFLICT (account_id, provider_message_id) DO UPDATE
                        SET folder_provider_id = EXCLUDED.folder_provider_id,
                            folder_display_name = EXCLUDED.folder_display_name,
                            subject_preview = EXCLUDED.subject_preview,
                            received_at = EXCLUDED.received_at,
                            is_unread = EXCLUDED.is_unread,
                            has_attachments = EXCLUDED.has_attachments,
                            last_seen_at = now()
                        """,
                        (
                            str(uuid.uuid4()),
                            account_id,
                            provider_message_id,
                            message.get("parentFolderId"),
                            message.get("_folder_display_name"),
                            _truncate_subject(message.get("subject")),
                            received_at,
                            bool(message.get("isRead") is False),
                            bool(message.get("hasAttachments")),
                        ),
                    )
                    persisted += 1
            connection.commit()
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to persist message sightings to PostgreSQL.",
        ) from exc

    return (len(messages), persisted)


async def _list_recent_messages(
    access_token: str,
    folder_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch recent message metadata from Graph for a folder (or All mail)."""
    select = (
        "id,parentFolderId,subject,receivedDateTime,isRead,hasAttachments,sentDateTime"
    )
    params: dict[str, Any] = {
        "$select": select,
        "$top": str(limit),
        "$orderby": "receivedDateTime desc",
    }
    path = f"/me/mailFolders/{folder_id}/messages" if folder_id else "/me/messages"
    payload = await _graph_get(access_token, path, params=params)
    return payload.get("value", [])


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/config-check")
def config_check() -> dict[str, Any]:
    return _session_payload()


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
def microsoft_start(login_hint: str | None = Query(default=None)) -> Response:
    state = secrets.token_urlsafe(24)
    verifier = _code_verifier()
    hint = login_hint.strip() if login_hint else None
    authorize_url = _authorize_url(state, _code_challenge(verifier), login_hint=hint or None)
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
    return response


@app.get("/auth/microsoft/callback")
async def microsoft_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    stored_state: str | None = Cookie(default=None, alias=AUTH_COOKIE),
    stored_verifier: str | None = Cookie(default=None, alias=PKCE_COOKIE),
) -> Response:
    if error:
        description = error_description or "Microsoft returned an authorization error."
        return RedirectResponse(_web_redirect("error", reason=description), status_code=302)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state.")

    if not stored_state or state != stored_state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch.")

    if not stored_verifier:
        raise HTTPException(status_code=400, detail="Missing PKCE verifier.")

    token_response = await _exchange_code(code, stored_verifier)
    profile = await _graph_profile(token_response["access_token"])
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
    return redirect


@app.post("/auth/logout")
def auth_logout() -> Response:
    response = JSONResponse({"status": "signed_out"})
    response.delete_cookie(EMAIL_COOKIE)
    response.delete_cookie(NAME_COOKIE)
    response.delete_cookie(AUTH_COOKIE)
    response.delete_cookie(PKCE_COOKIE)
    return response


@app.get("/mail/folders")
async def mail_folders(
    include_hidden: bool = Query(default=False),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    if not linked_email:
        raise HTTPException(status_code=401, detail="No linked account session found.")

    access_token, account = await _graph_access_token_for_email(linked_email)
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
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    if not linked_email:
        raise HTTPException(status_code=401, detail="No linked account session found.")

    access_token, account = await _graph_access_token_for_email(linked_email)
    started_at = _utcnow()
    try:
        folders = _annotate_folders(await _ensure_default_mail_folders(access_token))
        _persist_folder_inventory(account["account_id"], folders)
    except HTTPException as exc:
        _record_sync_event(
            account["account_id"],
            "folder.bootstrap",
            status="error",
            started_at=started_at,
            errors=1,
            error_message=str(exc.detail),
        )
        raise
    _record_sync_event(
        account["account_id"],
        "folder.bootstrap",
        status="success",
        started_at=started_at,
        folders_seen=len(folders),
    )
    return {
        "account": {
            "email": account["email"],
            "display_name": account["display_name"],
        },
        "ensured_folders": folders,
    }


@app.post("/mail/folders/inventory/sync")
async def sync_mail_folder_inventory(
    include_hidden: bool = Query(default=True),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    if not linked_email:
        raise HTTPException(status_code=401, detail="No linked account session found.")

    access_token, account = await _graph_access_token_for_email(linked_email)
    started_at = _utcnow()
    try:
        folders = _annotate_folders(
            await _list_mail_folders(access_token, include_hidden=include_hidden)
        )
        _persist_folder_inventory(account["account_id"], folders)
    except HTTPException as exc:
        _record_sync_event(
            account["account_id"],
            "folder.inventory.sync",
            status="error",
            started_at=started_at,
            errors=1,
            error_message=str(exc.detail),
        )
        raise
    _record_sync_event(
        account["account_id"],
        "folder.inventory.sync",
        status="success",
        started_at=started_at,
        folders_seen=len(folders),
    )
    return {
        "account": {
            "email": account["email"],
            "display_name": account["display_name"],
        },
        "folders": folders,
    }


@app.post("/mail/messages/sync")
async def sync_mail_messages(
    folder_id: str | None = Query(default=None),
    limit: int = Query(default=MESSAGE_SYNC_DEFAULT_LIMIT, ge=1, le=MESSAGE_SYNC_MAX_LIMIT),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    """Sample recent messages from Graph and persist metadata only.

    Pulls the most recently received messages for the linked account. By
    default scans across all folders (Graph's `/me/messages`); pass
    `folder_id` to scope the pull. Bodies are never fetched or stored;
    subjects are truncated. The endpoint records a sync_event regardless of
    success/failure so the operations dashboard reflects the call.
    """
    if not linked_email:
        raise HTTPException(status_code=401, detail="No linked account session found.")

    access_token, account = await _graph_access_token_for_email(linked_email)
    started_at = _utcnow()
    folder_display_name: str | None = None
    if folder_id:
        # Best-effort folder name lookup so sightings can carry a friendly
        # label without an extra Graph round-trip per message.
        try:
            folder = await _graph_get(access_token, f"/me/mailFolders/{folder_id}")
            folder_display_name = folder.get("displayName")
        except HTTPException:
            folder_display_name = None

    try:
        messages = await _list_recent_messages(access_token, folder_id, limit)
        if folder_display_name:
            for message in messages:
                message["_folder_display_name"] = folder_display_name
        seen, persisted = _persist_message_sightings(account["account_id"], messages)
    except HTTPException as exc:
        _record_sync_event(
            account["account_id"],
            "messages.sync",
            status="error",
            started_at=started_at,
            errors=1,
            error_message=str(exc.detail),
            detail={"folder_id": folder_id, "limit": limit},
        )
        raise

    _record_sync_event(
        account["account_id"],
        "messages.sync",
        status="success",
        started_at=started_at,
        messages_seen=seen,
        messages_persisted=persisted,
        detail={
            "folder_id": folder_id,
            "folder_display_name": folder_display_name,
            "limit": limit,
        },
    )

    return {
        "account": {
            "email": account["email"],
            "display_name": account["display_name"],
        },
        "scope": {
            "folder_id": folder_id,
            "folder_display_name": folder_display_name,
            "limit": limit,
        },
        "messages_seen": seen,
        "messages_persisted": persisted,
    }


@app.get("/mail/folders/inventory")
async def mail_folder_inventory(
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    if not linked_email:
        raise HTTPException(status_code=401, detail="No linked account session found.")

    account = _load_account_credentials(linked_email)
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
    "No message sightings have been persisted yet. Run POST /mail/messages/sync "
    "(or use the Sync messages action in API/Diagnostics) to start populating "
    "message_sighting rows for this account."
)
PENDING_REASON_ACTIONS = (
    "Automated message movement is not yet implemented. The mailbox_action_event "
    "table exists for instrumentation, but no worker writes to it; counts here "
    "will stay zero until the move worker lands."
)
DEFAULT_DASHBOARD_WINDOW_DAYS = 7
SUPPORTED_DASHBOARD_WINDOW_DAYS = (1, 7, 30)


def _normalize_window_days(window_days: int | None) -> int:
    """Clamp the dashboard window to the supported set.

    The dashboard offers 24h / 7d / 30d. Anything else is coerced into the
    nearest supported bucket so callers can pass a free-form integer without
    surprising results.
    """
    if not window_days or window_days <= 0:
        return DEFAULT_DASHBOARD_WINDOW_DAYS
    if window_days <= 1:
        return 1
    if window_days <= 7:
        return 7
    return 30


def _empty_volume_metrics(window_days: int = DEFAULT_DASHBOARD_WINDOW_DAYS) -> dict[str, Any]:
    return {
        "available": False,
        "reason": PENDING_REASON_INGESTION,
        "window_days": window_days,
        "messages_in": 0,
        "messages_persisted": 0,
        "errors": 0,
        "last_message_received_at": None,
        "last_sync_at": None,
        "last_sync_status": None,
        "last_sync_error": None,
        "by_day": [],
        "by_folder": [],
    }


def _empty_action_metrics(window_days: int = DEFAULT_DASHBOARD_WINDOW_DAYS) -> dict[str, Any]:
    return {
        "available": False,
        "reason": PENDING_REASON_ACTIONS,
        "window_days": window_days,
        "actions_recommended": 0,
        "actions_executed": 0,
        "actions_failed": 0,
        "messages_moved": 0,
        "last_action_at": None,
    }


def _load_volume_metrics(account_id: str, window_days: int) -> dict[str, Any]:
    """Compute message-volume metrics from real persisted rows.

    Returns the same shape as `_empty_volume_metrics` but with `available`
    flipped to True when there is at least one sync event or sighting in the
    window. We surface even an empty-but-recently-synced state as available
    so the dashboard can distinguish "no instrumentation" from "synced, no
    new messages".
    """
    if not _database_url():
        return _empty_volume_metrics(window_days)
    try:
        _ensure_account_tables()
    except Exception:
        return _empty_volume_metrics(window_days)

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*),
                           max(received_at),
                           max(first_seen_at)
                    FROM message_sighting
                    WHERE account_id = %s
                      AND received_at >= now() - make_interval(days => %s)
                    """,
                    (account_id, window_days),
                )
                count_row = cursor.fetchone()
                messages_in = int(count_row[0] or 0)
                last_received = count_row[1]
                last_seen = count_row[2]

                cursor.execute(
                    """
                    SELECT count(*) FROM message_sighting WHERE account_id = %s
                    """,
                    (account_id,),
                )
                total_persisted = int((cursor.fetchone() or [0])[0])

                cursor.execute(
                    """
                    SELECT to_char(date_trunc('day', received_at), 'YYYY-MM-DD') AS day,
                           count(*)
                    FROM message_sighting
                    WHERE account_id = %s
                      AND received_at >= now() - make_interval(days => %s)
                    GROUP BY 1
                    ORDER BY 1 ASC
                    """,
                    (account_id, window_days),
                )
                by_day = [
                    {"day": row[0], "messages_in": int(row[1])}
                    for row in cursor.fetchall()
                    if row[0] is not None
                ]

                cursor.execute(
                    """
                    SELECT
                        COALESCE(folder_display_name, folder_provider_id, '(unknown)'),
                        count(*)
                    FROM message_sighting
                    WHERE account_id = %s
                      AND received_at >= now() - make_interval(days => %s)
                    GROUP BY 1
                    ORDER BY 2 DESC
                    LIMIT 12
                    """,
                    (account_id, window_days),
                )
                by_folder = [
                    {"folder": row[0], "messages_in": int(row[1])}
                    for row in cursor.fetchall()
                ]

                cursor.execute(
                    """
                    SELECT completed_at, status, error_message,
                           messages_seen, messages_persisted
                    FROM sync_event
                    WHERE account_id = %s AND operation = 'messages.sync'
                    ORDER BY completed_at DESC
                    LIMIT 1
                    """,
                    (account_id,),
                )
                last_sync_row = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT count(*)
                    FROM sync_event
                    WHERE account_id = %s
                      AND operation = 'messages.sync'
                      AND status = 'error'
                      AND completed_at >= now() - make_interval(days => %s)
                    """,
                    (account_id, window_days),
                )
                error_count = int((cursor.fetchone() or [0])[0])
    except psycopg.Error:
        return _empty_volume_metrics(window_days)

    last_sync_at = last_sync_row[0].isoformat() if last_sync_row and last_sync_row[0] else None
    last_sync_status = last_sync_row[1] if last_sync_row else None
    last_sync_error = last_sync_row[2] if last_sync_row else None

    available = bool(total_persisted) or bool(last_sync_at)

    return {
        "available": available,
        "reason": None if available else PENDING_REASON_INGESTION,
        "window_days": window_days,
        "messages_in": messages_in,
        "messages_persisted": total_persisted,
        "errors": error_count,
        "last_message_received_at": last_received.isoformat() if last_received else None,
        "last_message_seen_at": last_seen.isoformat() if last_seen else None,
        "last_sync_at": last_sync_at,
        "last_sync_status": last_sync_status,
        "last_sync_error": last_sync_error,
        "by_day": by_day,
        "by_folder": by_folder,
    }


def _load_action_metrics(account_id: str, window_days: int) -> dict[str, Any]:
    """Compute action/move metrics from mailbox_action_event.

    Today the table is empty (no automated move worker), so this typically
    returns the empty/pending shape with `available=False`. Once the worker
    starts writing rows, the same query path lights up automatically.
    """
    if not _database_url():
        return _empty_action_metrics(window_days)
    try:
        _ensure_account_tables()
    except Exception:
        return _empty_action_metrics(window_days)

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*) FILTER (WHERE status = 'executed'),
                           count(*) FILTER (WHERE status = 'failed'),
                           count(*) FILTER (WHERE status = 'recommended'),
                           count(*) FILTER (
                               WHERE status = 'executed' AND action_type = 'move'
                           ),
                           max(occurred_at)
                    FROM mailbox_action_event
                    WHERE account_id = %s
                      AND occurred_at >= now() - make_interval(days => %s)
                    """,
                    (account_id, window_days),
                )
                row = cursor.fetchone() or (0, 0, 0, 0, None)
                cursor.execute(
                    "SELECT count(*) FROM mailbox_action_event WHERE account_id = %s",
                    (account_id,),
                )
                total = int((cursor.fetchone() or [0])[0])
    except psycopg.Error:
        return _empty_action_metrics(window_days)

    available = bool(total)
    return {
        "available": available,
        "reason": None if available else PENDING_REASON_ACTIONS,
        "window_days": window_days,
        "actions_executed": int(row[0] or 0),
        "actions_failed": int(row[1] or 0),
        "actions_recommended": int(row[2] or 0),
        "messages_moved": int(row[3] or 0),
        "last_action_at": row[4].isoformat() if row[4] else None,
    }


def _load_recent_sync_events(
    account_id: str, window_days: int, limit: int = 50
) -> list[dict[str, Any]]:
    if not _database_url():
        return []
    try:
        _ensure_account_tables()
    except Exception:
        return []

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT operation, status, folders_seen, messages_seen,
                           messages_persisted, errors, error_message,
                           started_at, completed_at
                    FROM sync_event
                    WHERE account_id = %s
                      AND completed_at >= now() - make_interval(days => %s)
                    ORDER BY completed_at DESC
                    LIMIT %s
                    """,
                    (account_id, window_days, limit),
                )
                rows = cursor.fetchall()
    except psycopg.Error:
        return []

    return [
        {
            "operation": row[0],
            "status": row[1],
            "folders_seen": row[2],
            "messages_seen": row[3],
            "messages_persisted": row[4],
            "errors": int(row[5] or 0),
            "error_message": row[6],
            "started_at": row[7].isoformat() if row[7] else None,
            "occurred_at": row[8].isoformat() if row[8] else None,
        }
        for row in rows
    ]


def _list_user_accounts(user_email: str) -> list[dict[str, Any]]:
    if not _database_url():
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
                    WHERE au.email = %s
                    ORDER BY ca.updated_at DESC
                    """,
                    (user_email,),
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


def _account_dashboard_payload(
    account_row: dict[str, Any],
    window_days: int = DEFAULT_DASHBOARD_WINDOW_DAYS,
) -> dict[str, Any]:
    folder_summary = _summarize_folder_inventory(account_row["account_id"])
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
        "email_volume": _load_volume_metrics(account_row["account_id"], window_days),
        "action_activity": _load_action_metrics(account_row["account_id"], window_days),
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
    window_days: int = Query(default=DEFAULT_DASHBOARD_WINDOW_DAYS, ge=1, le=90),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
    linked_name: str | None = Cookie(default=None, alias=NAME_COOKIE),
) -> dict[str, Any]:
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)
    window = _normalize_window_days(window_days)

    if rows:
        per_account = [_account_dashboard_payload(row, window_days=window) for row in rows]
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
                "email_volume": _empty_volume_metrics(window),
                "action_activity": _empty_action_metrics(window),
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
        "messages_in": sum(
            entry["email_volume"].get("messages_in") or 0 for entry in per_account
        ),
        "messages_persisted": sum(
            entry["email_volume"].get("messages_persisted") or 0 for entry in per_account
        ),
        "messages_moved": sum(
            entry["action_activity"].get("messages_moved") or 0 for entry in per_account
        ),
        "errors": sum(entry["email_volume"].get("errors") or 0 for entry in per_account),
    }

    return {
        "generated_at": _utcnow().isoformat(),
        "user": {"email": user_email},
        "window_days": window,
        "supported_window_days": list(SUPPORTED_DASHBOARD_WINDOW_DAYS),
        "totals": totals,
        "accounts": per_account,
        "pending_instrumentation": [
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
# surfaces folder-inventory changes (the only actions the runtime currently
# performs); message movement instrumentation is reported as pending until
# the connector worker lands. Alerts are computed from current state — no
# fabricated/example notices.

PENDING_REASON_MESSAGE_MOVEMENT = (
    "Automated message movement is not yet implemented. The mailbox_action_event "
    "table exists for instrumentation, but no worker writes to it; this feed "
    "will populate once the move worker lands."
)
NO_RECENT_SYNC_THRESHOLD_HOURS = 24


def _load_message_movement_events(
    account_id: str, window_days: int, limit: int = 25
) -> list[dict[str, Any]]:
    if not _database_url():
        return []
    try:
        _ensure_account_tables()
    except Exception:
        return []

    psycopg = _psycopg()
    try:
        with _get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT action_type, status, source_folder, target_folder,
                           provider_message_id, error_message, occurred_at
                    FROM mailbox_action_event
                    WHERE account_id = %s
                      AND occurred_at >= now() - make_interval(days => %s)
                    ORDER BY occurred_at DESC
                    LIMIT %s
                    """,
                    (account_id, window_days, limit),
                )
                rows = cursor.fetchall()
    except psycopg.Error:
        return []

    return [
        {
            "event_type": f"action.{row[0]}",
            "status": row[1],
            "source_folder": row[2],
            "target_folder": row[3],
            "provider_message_id": row[4],
            "error_message": row[5],
            "occurred_at": row[6].isoformat() if row[6] else None,
        }
        for row in rows
    ]


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


@app.get("/activity")
def activity_log(
    limit: int = Query(default=25, ge=1, le=100),
    window_days: int = Query(default=DEFAULT_DASHBOARD_WINDOW_DAYS, ge=1, le=90),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)
    window = _normalize_window_days(window_days)

    folder_events: list[dict[str, Any]] = []
    sync_events: list[dict[str, Any]] = []
    movement_events: list[dict[str, Any]] = []
    folder_available = False
    sync_available = False
    movement_available = False
    for row in rows:
        events = _load_folder_activity(row["account_id"], limit=limit)
        for event in events:
            event["account"] = {
                "account_id": row["account_id"],
                "email": row["email"],
            }
            folder_events.append(event)
        folder_available = True

        for sync in _load_recent_sync_events(row["account_id"], window, limit=limit):
            sync["account"] = {
                "account_id": row["account_id"],
                "email": row["email"],
            }
            sync_events.append(sync)
        sync_available = True

        for movement in _load_message_movement_events(row["account_id"], window, limit=limit):
            movement["account"] = {
                "account_id": row["account_id"],
                "email": row["email"],
            }
            movement_events.append(movement)

    folder_events.sort(key=lambda event: event.get("occurred_at") or "", reverse=True)
    folder_events = folder_events[:limit]
    sync_events.sort(key=lambda event: event.get("occurred_at") or "", reverse=True)
    sync_events = sync_events[:limit]
    movement_events.sort(key=lambda event: event.get("occurred_at") or "", reverse=True)
    movement_events = movement_events[:limit]
    movement_available = bool(movement_events)

    folder_reason: str | None = None
    if not folder_available:
        folder_reason = (
            "No connected_account rows for this session; folder activity "
            "becomes available once accounts are persisted via the OAuth "
            "callback with DATABASE_URL configured."
        )

    sync_reason: str | None = None
    if not sync_available:
        sync_reason = (
            "No connected_account rows for this session; sync events become "
            "available once a folder/message sync runs against a persisted "
            "account."
        )

    return {
        "generated_at": _utcnow().isoformat(),
        "user": {"email": user_email},
        "window_days": window,
        "supported_window_days": list(SUPPORTED_DASHBOARD_WINDOW_DAYS),
        "sync_activity": {
            "available": sync_available,
            "reason": sync_reason,
            "events": sync_events,
        },
        "folder_activity": {
            "available": folder_available,
            "reason": folder_reason,
            "events": folder_events,
        },
        "message_movement": {
            "available": movement_available,
            "reason": None if movement_available else PENDING_REASON_MESSAGE_MOVEMENT,
            "events": movement_events,
        },
        "pending_instrumentation": (
            [{"metric": "message_movement", "reason": PENDING_REASON_MESSAGE_MOVEMENT}]
            if not movement_available
            else []
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

    if _database_url():
        for account in accounts:
            account_id = account.get("account_id")
            email_addr = account.get("email")
            if not account_id:
                continue

            volume = _load_volume_metrics(account_id, DEFAULT_DASHBOARD_WINDOW_DAYS)
            last_sync_iso = volume.get("last_sync_at")
            last_sync_status = volume.get("last_sync_status")

            if not last_sync_iso:
                alerts.append(
                    {
                        "code": "no_message_sync_yet",
                        "severity": "info",
                        "message": (
                            f"No message sync has run for {email_addr} yet. "
                            "Email volume tiles will stay at zero until a sync runs."
                        ),
                        "next_action": (
                            "Run POST /mail/messages/sync (Sync messages action in "
                            "API/Diagnostics) to start populating message_sighting rows."
                        ),
                        "context": {"email": email_addr},
                    }
                )
            else:
                last_sync_dt = _parse_graph_datetime(last_sync_iso)
                stale = (
                    last_sync_dt is not None
                    and last_sync_dt < _utcnow() - timedelta(hours=NO_RECENT_SYNC_THRESHOLD_HOURS)
                )
                if stale:
                    alerts.append(
                        {
                            "code": "stale_message_sync",
                            "severity": "warning",
                            "message": (
                                f"Last message sync for {email_addr} ran more than "
                                f"{NO_RECENT_SYNC_THRESHOLD_HOURS}h ago "
                                f"({last_sync_iso})."
                            ),
                            "next_action": (
                                "Re-run the message sync to pick up new mail and "
                                "refresh the dashboard counts."
                            ),
                            "context": {"email": email_addr, "last_sync_at": last_sync_iso},
                        }
                    )
                if last_sync_status == "error":
                    alerts.append(
                        {
                            "code": "recent_message_sync_error",
                            "severity": "error",
                            "message": (
                                f"The most recent message sync for {email_addr} failed: "
                                f"{volume.get('last_sync_error') or 'unknown error'}"
                            ),
                            "next_action": (
                                "Check the API logs and retry the sync. If the "
                                "failure persists, re-run Microsoft sign-in."
                            ),
                            "context": {"email": email_addr},
                        }
                    )
                if (volume.get("messages_persisted") or 0) == 0 and last_sync_status == "success":
                    alerts.append(
                        {
                            "code": "no_messages_seen",
                            "severity": "info",
                            "message": (
                                f"Sync ran for {email_addr} but no messages were "
                                "persisted. The mailbox may be empty or the scope "
                                "needs widening."
                            ),
                            "next_action": (
                                "Try a larger limit, or sync a specific folder via "
                                "POST /mail/messages/sync?folder_id=…"
                            ),
                            "context": {"email": email_addr},
                        }
                    )

    alerts.append(
        {
            "code": "move_worker_pending",
            "severity": "info",
            "message": (
                "Automated message movement is not yet implemented. The Moved "
                "tile and Action activity will stay at zero until the move "
                "worker writes mailbox_action_event rows."
            ),
            "next_action": (
                "Track follow-on work in docs/dashboard-instrumentation.md → "
                "remaining instrumentation."
            ),
        }
    )

    return alerts


@app.get("/alerts")
def alerts(
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)
    if rows:
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
        bool(os.getenv(name)) for name, _ in _RUNTIME_VARIABLES if name != "KEY_VAULT_REFS_ENABLED"
    )

    items = _compute_alerts(accounts, runtime_present)
    counts = {"error": 0, "warning": 0, "info": 0}
    for item in items:
        severity = item.get("severity", "info")
        counts[severity] = counts.get(severity, 0) + 1

    return {
        "generated_at": _utcnow().isoformat(),
        "user": {"email": user_email},
        "counts": counts,
        "alerts": items,
    }


@app.get("/accounts/{email}/dashboard")
def account_dashboard(
    email: str,
    window_days: int = Query(default=DEFAULT_DASHBOARD_WINDOW_DAYS, ge=1, le=90),
    linked_email: str | None = Cookie(default=None, alias=EMAIL_COOKIE),
) -> dict[str, Any]:
    user_email = _resolve_session_user_email(linked_email)
    rows = _list_user_accounts(user_email)
    window = _normalize_window_days(window_days)

    target = next((row for row in rows if row["email"].lower() == email.lower()), None)
    if not target:
        raise HTTPException(
            status_code=404,
            detail="No connected account with that email is linked to this session.",
        )

    return {
        "generated_at": _utcnow().isoformat(),
        "user": {"email": user_email},
        "window_days": window,
        "supported_window_days": list(SUPPORTED_DASHBOARD_WINDOW_DAYS),
        **_account_dashboard_payload(target, window_days=window),
        "pending_instrumentation": [
            {"metric": "action_activity", "reason": PENDING_REASON_ACTIONS},
        ],
    }
