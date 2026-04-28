"""Static checks that the web UI exposes the inbox dry-run controls.

These don't run JS — they parse `apps/web/index.html` and assert the
elements / wiring needed for the Inbox Sorting tab are present. Pure
end-to-end browser tests would need Playwright; for CI we cover the
contract between the Python API endpoints and the static markup the
operator interacts with.
"""

from __future__ import annotations

from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parents[2] / "apps" / "web" / "index.html"


def _read_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_sorting_tab_button_exists():
    html = _read_html()
    assert 'data-testid="tab-sorting"' in html
    assert 'data-tab="sorting"' in html
    assert 'aria-controls="panel-sorting"' in html


def test_sorting_panel_and_run_controls_exist():
    html = _read_html()
    assert 'id="panel-sorting"' in html
    assert 'data-testid="sorting-panel"' in html
    assert 'data-testid="sorting-run-button"' in html
    assert 'data-testid="sorting-refresh-button"' in html
    assert 'data-testid="sorting-table-body"' in html
    assert 'data-testid="sorting-status"' in html


def test_sorting_panel_has_nondestructive_banner():
    html = _read_html()
    assert 'data-testid="sorting-nondestructive-banner"' in html
    # Phrasing must make it unambiguous nothing is moved or sent.
    assert "No messages are moved" in html
    assert "10 - Review" in html


def test_sorting_run_button_calls_classify_dryrun_endpoint():
    html = _read_html()
    # The JS uses these exact paths to call the persisted dry-run API.
    assert "/mail/inbox/classify-dryrun" in html
    assert "/mail/inbox/classify-dryrun/log" in html


def test_sorting_table_columns_cover_all_classifier_signals():
    html = _read_html()
    # Headers the operator needs for the review-first workflow.
    for header in (
        "Received",
        "From / Subject",
        "Recommendation",
        "Confidence",
        "Reasons / Flags",
        "Status",
    ):
        assert f"<th>{header}</th>" in html


def test_dashboard_has_sorting_summary_card():
    html = _read_html()
    # The dashboard tab must show the user that sorting is exposed.
    assert 'data-testid="dashboard-sorting-card"' in html
    assert 'data-testid="dashboard-sorting-open"' in html
    assert 'data-testid="dashboard-sorting-summary"' in html


def test_sorting_uses_selected_account_state():
    html = _read_html()
    # The JS reads accountState.selectedEmail when calling /classify-dryrun.
    assert "accountState.selectedEmail" in html
    # The URL is built from the selected account, not a hardcoded address.
    assert "encodeURIComponent(account)" in html


def test_default_login_hint_is_daniel_at_danielyoung_io():
    html = _read_html()
    # The "Connect" buttons may target a secondary mailbox, but the operator
    # signs in as daniel@danielyoung.io for the bring-up — make sure that
    # email is referenced in the page so the operator path stays documented.
    assert "danielyoung.io" in html


def test_primary_sign_in_uses_danielyoung_io_login_hint():
    html = _read_html()
    # The gate / header "Sign in with Microsoft" CTA should pre-fill the
    # primary tenant account so operators land on the right Microsoft
    # picker without typing.
    assert 'PRIMARY_LOGIN_HINT = "daniel@danielyoung.io"' in html
    assert "gateConnectLink.href = PRIMARY_SIGN_IN_URL" in html
    assert "headerSignInLink.href = PRIMARY_SIGN_IN_URL" in html


def test_login_screen_does_not_show_private_app_warning():
    html = _read_html()
    # The login UI should feel like a standard Microsoft sign-in, not a
    # gated private-app warning.
    assert "Private app" not in html
    assert "authorized account only" not in html


def test_noindex_meta_is_preserved():
    html = _read_html()
    # Security/SEO posture: the app must remain unindexed.
    assert 'name="robots"' in html
    assert "noindex" in html


def test_dhw_connect_button_keeps_dhw_login_hint():
    html = _read_html()
    # DHW add-account flow must keep its own login_hint so operators can
    # link the digitalhealthworks.com mailbox without retyping.
    assert 'DHW_LOGIN_HINT = "daniel.young@digitalhealthworks.com"' in html
    assert "connectDhwLink.href = DHW_SIGN_IN_URL" in html
    assert "accountNavConnectLink.href = DHW_SIGN_IN_URL" in html


def test_generic_connect_account_link_has_no_login_hint():
    html = _read_html()
    # "Connect a different account" must hit /auth/microsoft/start with no
    # hint so external tenants (e.g. BoldWorks) can still complete sign-in.
    assert "connectGenericLink.href = SIGN_IN_URL" in html
