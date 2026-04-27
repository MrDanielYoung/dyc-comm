import base64
import hashlib
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


def _authorize_url(state: str, code_challenge: str) -> str:
    tenant_id = _require_env("MICROSOFT_ENTRA_TENANT_ID")
    client_id = _require_env("MICROSOFT_ENTRA_CLIENT_ID")
    redirect_uri = _require_env("MICROSOFT_ENTRA_REDIRECT_URI")
    params = {
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
def microsoft_start() -> Response:
    state = secrets.token_urlsafe(24)
    verifier = _code_verifier()
    authorize_url = _authorize_url(state, _code_challenge(verifier))
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
