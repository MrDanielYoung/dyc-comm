import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_BASE = os.getenv("DYC_API_BASE_URL", "http://127.0.0.1:8000")
DEFAULT_STATE_DIR = Path(os.getenv("DYC_STATE_DIR", ".dyc"))
DEFAULT_COOKIE_FILE = DEFAULT_STATE_DIR / "cookies.json"


def _client(api_base: str, cookie_file: Path) -> httpx.Client:
    client = httpx.Client(base_url=api_base, follow_redirects=False, timeout=20.0)
    if cookie_file.exists():
        try:
            cookies = json.loads(cookie_file.read_text())
            client.cookies.update(cookies)
        except json.JSONDecodeError:
            pass
    return client


def _save_cookies(client: httpx.Client, cookie_file: Path) -> None:
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    cookie_file.write_text(json.dumps(dict(client.cookies.items()), indent=2))


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> Any:
    response = client.request(method, path, params=params, timeout=timeout)
    response.raise_for_status()
    if not response.text:
        return {}
    try:
        return response.json()
    except json.JSONDecodeError:
        return response.text


def _print(payload: Any) -> None:
    if isinstance(payload, str):
        print(payload)
        return
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_status(args: argparse.Namespace) -> int:
    with _client(args.api_base, args.cookie_file) as client:
        payload = _request(client, "GET", "/config-check")
        _save_cookies(client, args.cookie_file)
    _print(payload)
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    with _client(args.api_base, args.cookie_file) as client:
        payload = _request(client, "GET", "/auth/session")
        _save_cookies(client, args.cookie_file)
    _print(payload)
    return 0


def cmd_folders(args: argparse.Namespace) -> int:
    with _client(args.api_base, args.cookie_file) as client:
        payload = _request(
            client,
            "GET",
            "/mail/folders",
            params={"include_hidden": str(args.include_hidden).lower()},
        )
        _save_cookies(client, args.cookie_file)
    _print(payload)
    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    with _client(args.api_base, args.cookie_file) as client:
        payload = _request(client, "GET", "/mail/folders/inventory")
        _save_cookies(client, args.cookie_file)
    _print(payload)
    return 0


def cmd_inventory_sync(args: argparse.Namespace) -> int:
    with _client(args.api_base, args.cookie_file) as client:
        payload = _request(
            client,
            "POST",
            "/mail/folders/inventory/sync",
            params={"include_hidden": str(args.include_hidden).lower()},
        )
        _save_cookies(client, args.cookie_file)
    _print(payload)
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    with _client(args.api_base, args.cookie_file) as client:
        payload = _request(client, "POST", "/mail/folders/bootstrap")
        _save_cookies(client, args.cookie_file)
    _print(payload)
    return 0


def cmd_ingest_dry_run(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"limit": int(args.limit)}
    if args.email:
        params["email"] = args.email
    with _client(args.api_base, args.cookie_file) as client:
        # Operator runs may pull and classify up to 50 messages; bump the
        # per-call timeout above the 20s default to leave headroom.
        payload = _request(
            client,
            "POST",
            "/mail/messages/ingest-dry-run",
            params=params,
            timeout=120.0,
        )
        _save_cookies(client, args.cookie_file)
    _print(payload)
    return 0


def cmd_recommendations(args: argparse.Namespace) -> int:
    with _client(args.api_base, args.cookie_file) as client:
        payload = _request(
            client,
            "GET",
            "/mail/messages/recommendations",
            params={"limit": int(args.limit)},
        )
        _save_cookies(client, args.cookie_file)
    _print(payload)
    return 0


def cmd_auth_url(args: argparse.Namespace) -> int:
    with _client(args.api_base, args.cookie_file) as client:
        response = client.get("/auth/microsoft/start")
        _save_cookies(client, args.cookie_file)
    if response.status_code not in {301, 302, 307, 308}:
        print(response.text)
        return 1
    print(response.headers.get("location", ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dyc",
        description="DYC mailbox management CLI",
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help="Base URL for the DYC API",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        default=DEFAULT_COOKIE_FILE,
        help="Path to the persisted cookie file",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Show config-check output")
    status.set_defaults(func=cmd_status)

    session = subparsers.add_parser("session", help="Show current auth session")
    session.set_defaults(func=cmd_session)

    folders = subparsers.add_parser("folders", help="List mailbox folders")
    folders.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include hidden folders",
    )
    folders.set_defaults(func=cmd_folders)

    inventory = subparsers.add_parser("inventory", help="Show persisted folder inventory")
    inventory.set_defaults(func=cmd_inventory)

    inventory_sync = subparsers.add_parser("inventory-sync", help="Sync live folder inventory")
    inventory_sync.add_argument(
        "--include-hidden",
        action="store_true",
        default=True,
        help="Include hidden folders in inventory sync",
    )
    inventory_sync.set_defaults(func=cmd_inventory_sync)

    bootstrap = subparsers.add_parser("bootstrap", help="Ensure DYC folders exist")
    bootstrap.set_defaults(func=cmd_bootstrap)

    auth_url = subparsers.add_parser("auth-url", help="Print the Microsoft auth URL from the API")
    auth_url.set_defaults(func=cmd_auth_url)

    ingest_dry_run = subparsers.add_parser(
        "ingest-dry-run",
        help=(
            "Operator-triggered, non-destructive ingest of recent messages "
            "with dry-run classification. Does not move, send, or delete mail."
        ),
    )
    ingest_dry_run.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent messages to fetch (1-50, default 10)",
    )
    ingest_dry_run.add_argument(
        "--email",
        type=str,
        default=None,
        help="Account email; must match the signed-in session (e.g. daniel@danielyoung.io)",
    )
    ingest_dry_run.set_defaults(func=cmd_ingest_dry_run)

    recommendations = subparsers.add_parser(
        "recommendations",
        help="List recent dry-run classification recommendations for the signed-in account.",
    )
    recommendations.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum recommendations to return (1-200, default 50)",
    )
    recommendations.set_defaults(func=cmd_recommendations)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except httpx.HTTPStatusError as exc:
        message = exc.response.text or str(exc)
        print(message, file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
