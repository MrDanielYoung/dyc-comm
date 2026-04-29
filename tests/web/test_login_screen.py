"""Static checks for the gate / login screen markup and behavior tests
for the unauthenticated-redirect logic.

Static checks live first; behavior checks (which extract the JS
``decideSessionAction`` from ``index.html`` and execute it under Node)
follow. The behavior checks would have caught the live-site regression
where unauthenticated visits never auto-redirected to Microsoft, because
they exercise the actual JS rather than just grepping for substrings.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).resolve().parents[2] / "apps" / "web" / "index.html"


def _read_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_login_subtitle_is_removed():
    html = _read_html()
    assert "Private app" not in html
    assert "authorized account only" not in html
    assert 'id="gateCopy"' not in html
    assert "login-subtitle" not in html


def test_login_keeps_sign_in_title_and_microsoft_button():
    html = _read_html()
    assert 'id="gateTitle">Sign in</h1>' in html
    assert 'data-testid="login-button"' in html
    assert "Sign in with Microsoft" in html


def test_primary_login_uses_danielyoung_io_login_hint():
    """The primary gate + header sign-in entry points pre-fill the
    danielyoung.io account so the operator lands on the standard
    Microsoft picker for the right tenant. The 'Connect a different
    account' CTA must remain hint-free so additional account flows are
    not constrained."""
    html = _read_html()
    assert 'PRIMARY_LOGIN_HINT = "daniel@danielyoung.io"' in html
    assert "gateConnectLink.href = PRIMARY_SIGN_IN_URL" in html
    assert "headerSignInLink.href = PRIMARY_SIGN_IN_URL" in html
    # The "different account" link must still use the un-hinted URL.
    assert "connectGenericLink.href = SIGN_IN_URL" in html


# ---------------------------------------------------------------------------
# Behavior tests: extract ``decideSessionAction`` from index.html and run it
# under Node so we test what the browser will actually do, not just whether a
# string appears in the source.
# ---------------------------------------------------------------------------


def _extract_decide_session_action(html: str) -> str:
    """Pull the ``decideSessionAction`` function out of index.html.

    The function is intentionally written as a self-contained pure
    function (no DOM, no closures) so it can be lifted into a Node
    harness verbatim. We grab from ``function decideSessionAction``
    through the matching closing brace at the start of a line of the
    same indent (two spaces of indent inside the script tag → six
    spaces from column 0 since the script body is indented four).
    """
    match = re.search(
        r"^(      function decideSessionAction\(.*?\n      \})\n",
        html,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert match, "decideSessionAction function not found in index.html"
    return match.group(1)


def _run_node(js: str) -> dict:
    if shutil.which("node") is None:  # pragma: no cover - depends on CI image
        pytest.skip("node runtime not available")
    result = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise AssertionError(
            f"node failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


_HARNESS_TEMPLATE = """\
%(fn)s

const inputs = %(scenario)s;
inputs.reauthSignInUrl = (email) =>
  `https://api.example.com/auth/microsoft/start?login_hint=${encodeURIComponent(email)}`;
if (inputs.fetchError === 'throw') {
  inputs.fetchError = new Error(inputs.fetchErrorMessage || 'boom');
} else {
  inputs.fetchError = null;
}
const decision = decideSessionAction(inputs);
const out = { kind: decision.kind };
if (decision.url !== undefined) out.url = decision.url;
if (decision.chip !== undefined) out.chip = decision.chip;
if (decision.notice !== undefined) out.notice = decision.notice;
if (decision.account !== undefined) out.accountEmail = decision.account.email;
console.log(JSON.stringify(out));
"""


def _decide(scenario: dict) -> dict:
    """Run ``decideSessionAction`` under Node with the given inputs and
    return the decision object."""
    html = _read_html()
    fn = _extract_decide_session_action(html)
    harness = _HARNESS_TEMPLATE % {"fn": fn, "scenario": json.dumps(scenario)}
    return _run_node(harness)


PRIMARY_URL = "https://api.example.com/auth/microsoft/start?login_hint=daniel%40danielyoung.io"


def test_initial_unauthenticated_visit_redirects_to_microsoft():
    """A normal first visit with no linked account must redirect to the
    primary Microsoft sign-in URL — not render the in-app login card.
    This is the bug PR #49 tried to fix; the previous string-only test
    let the regression slip through."""
    decision = _decide(
        {
            "initial": True,
            "suppressRedirect": False,
            "payload": {"linked_account": None},
            "fetchError": None,
            "primarySignInUrl": PRIMARY_URL,
        }
    )
    assert decision["kind"] == "redirect"
    assert decision["url"] == PRIMARY_URL


def test_initial_visit_with_stale_cookie_redirects_for_reauth():
    """If a returning user still has the linked_email cookie but their
    refresh token has expired (mailbox_access_ready: false), the gate
    used to render a 'Re-authentication required' card and wait for the
    user to click. Match the unauthenticated path: redirect them to
    Microsoft directly with their email as the login_hint."""
    decision = _decide(
        {
            "initial": True,
            "suppressRedirect": False,
            "payload": {
                "linked_account": {"email": "operator@example.com"},
                "mailbox_access_ready": False,
            },
            "fetchError": None,
            "primarySignInUrl": PRIMARY_URL,
        }
    )
    assert decision["kind"] == "redirect"
    assert "login_hint=operator%40example.com" in decision["url"]


def test_auth_error_callback_does_not_redirect():
    """When we land back from Microsoft with ?auth=error, suppressRedirect
    is set so the user can read the failure notice instead of being
    bounced through Microsoft again in a loop."""
    decision = _decide(
        {
            "initial": True,
            "suppressRedirect": True,
            "payload": {"linked_account": None},
            "fetchError": None,
            "primarySignInUrl": PRIMARY_URL,
        }
    )
    assert decision["kind"] == "gate"
    assert decision["chip"] == "Sign-in required"


def test_api_unreachable_does_not_redirect():
    """If the session check itself fails (network/API down), we surface
    the error on the gate and do not silently navigate away — the user
    needs to see the failure to understand what's happening."""
    decision = _decide(
        {
            "initial": True,
            "suppressRedirect": False,
            "payload": None,
            "fetchError": "throw",
            "fetchErrorMessage": "Failed to fetch",
            "primarySignInUrl": PRIMARY_URL,
        }
    )
    assert decision["kind"] == "gate"
    assert decision["chip"] == "API unreachable"
    assert "Failed to fetch" in decision["notice"]


def test_retry_does_not_auto_redirect():
    """The retry/refresh buttons set suppressRedirect=true before
    re-evaluating, so even if the session is still unauthenticated the
    user stays on the gate (they explicitly chose to retry, not to be
    bounced)."""
    decision = _decide(
        {
            "initial": True,
            "suppressRedirect": True,
            "payload": {"linked_account": None},
            "fetchError": None,
            "primarySignInUrl": PRIMARY_URL,
        }
    )
    assert decision["kind"] == "gate"


def test_non_initial_evaluate_never_redirects():
    """Re-evaluations triggered after the user is already in the app
    (e.g. 401 fallback from a later API call) must never auto-redirect.
    The user is mid-task; yanking them to Microsoft would lose context."""
    decision = _decide(
        {
            "initial": False,
            "suppressRedirect": False,
            "payload": {"linked_account": None},
            "fetchError": None,
            "primarySignInUrl": PRIMARY_URL,
        }
    )
    assert decision["kind"] == "gate"


def test_ready_session_returns_app():
    """A fully linked, mailbox-ready session lands the user in the app."""
    decision = _decide(
        {
            "initial": True,
            "suppressRedirect": False,
            "payload": {
                "linked_account": {"email": "operator@example.com"},
                "mailbox_access_ready": True,
            },
            "fetchError": None,
            "primarySignInUrl": PRIMARY_URL,
        }
    )
    assert decision["kind"] == "app"
    assert decision["accountEmail"] == "operator@example.com"


# ---------------------------------------------------------------------------
# Static wiring checks: make sure ``evaluateSession`` and the page-load
# handlers wire suppressRedirect through correctly. These are cheap guards
# against accidental rewrites that would re-introduce the regression.
# ---------------------------------------------------------------------------


def test_evaluate_session_uses_decide_helper():
    html = _read_html()
    assert "function decideSessionAction(" in html
    assert "decideSessionAction({" in html
    # The runtime call passes the live suppress flag in.
    assert "suppressRedirect: suppressInitialRedirect" in html


def test_redirect_executes_window_location_replace():
    html = _read_html()
    # A ``redirect`` decision must actually navigate; if this line is
    # ever removed the auto-redirect silently regresses to a no-op.
    assert 'decision.kind === "redirect"' in html
    assert "window.location.replace(decision.url)" in html


def test_auth_error_branch_sets_suppress_flag():
    html = _read_html()
    assert 'url.searchParams.get("auth") === "error"' in html
    # The flag must be set in the auth-error branch so the subsequent
    # evaluateSession({initial: true}) does not redirect.
    error_branch = html.split('url.searchParams.get("auth") === "error"', 1)[1]
    assert "suppressInitialRedirect = true" in error_branch.split("evaluateSession", 1)[0]


def test_signout_sets_suppress_flag():
    html = _read_html()
    after_signout = html.split("async function signOut()", 1)[1]
    signout_block = after_signout.split("async function safeCallApi", 1)[0]
    assert "suppressInitialRedirect = true" in signout_block


def test_retry_and_refresh_handlers_set_suppress_flag():
    html = _read_html()
    # Both gateRetryButton and refreshButton handlers must mark the
    # session as user-driven before re-evaluating.
    for handler in ("gateRetryButton.addEventListener", "refreshButton.addEventListener"):
        block = html.split(handler, 1)[1].split("addEventListener", 1)[0]
        assert "suppressInitialRedirect = true" in block, (
            f"{handler} should set suppressInitialRedirect before evaluateSession"
        )


def test_initial_page_load_calls_evaluate_session():
    html = _read_html()
    # The bottom-of-script call kicks off the redirect flow on first paint.
    assert "evaluateSession({ initial: true })" in html
