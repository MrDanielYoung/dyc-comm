"""Static checks for the gate / login screen markup.

We render the standard Microsoft login look on the gate view: just the
logo, "Sign in" title, and "Sign in with Microsoft" button. The previous
"Private app — authorized account only." subtitle is intentionally gone.
"""

from __future__ import annotations

from pathlib import Path

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
