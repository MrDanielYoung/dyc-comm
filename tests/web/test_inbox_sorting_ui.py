"""Static checks that the web UI exposes inbox sorting controls.

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


def test_sorting_panel_exists_without_diagnostic_run_controls():
    html = _read_html()
    assert 'id="panel-sorting"' in html
    assert 'data-testid="sorting-panel"' in html
    assert 'data-testid="sorting-refresh-button"' in html
    assert 'data-testid="sorting-table-body"' in html
    assert 'data-testid="sorting-status"' in html
    assert 'data-testid="sorting-run-button"' not in html
    assert 'data-testid="sorting-automation-button"' not in html


def test_sorting_panel_has_nondestructive_banner():
    html = _read_html()
    assert 'data-testid="sorting-nondestructive-banner"' in html
    # Phrasing must make it unambiguous nothing is moved or sent.
    assert "dry-run itself never moves" in html
    assert "10 - Review" in html


def test_diagnostic_controls_call_sorting_endpoints():
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


def test_sorting_table_uses_readable_layout_and_24_hour_time():
    html = _read_html()
    assert "min-width: 1180px" in html
    assert "table-layout: fixed" in html
    assert "hour12: false" in html


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


def test_sorting_panel_exposes_setup_cta_for_unready_accounts():
    """When the selected account is linked but mailbox-access is not ready,
    the Sorting tab must show a clear setup notice + reconnect CTA, not a
    silent run button. This is the stabilization promise: actionable setup
    controls instead of a hidden sorting UI.
    """
    html = _read_html()
    assert 'data-testid="sorting-setup-notice"' in html
    assert 'data-testid="sorting-reconnect"' in html
    # The notice must use the term "Reconnect" so the operator knows the
    # action they need to take.
    assert "Reconnect this account" in html


def test_dashboard_sorting_card_exposes_reconnect_for_unready_accounts():
    html = _read_html()
    assert 'data-testid="dashboard-sorting-reconnect"' in html
    assert 'data-testid="dashboard-sorting-setup-notice"' in html


def test_sorting_run_button_is_blocked_when_account_not_mailbox_ready():
    """The JS must hard-disable the run path when the selected account
    has mailbox_access_ready=false. It is not enough to rely on the API
    returning 409 — the operator should not click into a failing call.
    """
    html = _read_html()
    # The readiness predicate referenced from the run/log paths.
    assert "selectedAccountIsMailboxReady" in html
    # The runSortingDryRun guard refuses to call the endpoint when not ready.
    assert "Mailbox access for" in html
    # The reconnect link is rebuilt from the selected account email so the
    # OAuth start URL carries the right login_hint.
    assert "signInUrlForEmail" in html


def test_login_button_present_on_initial_load():
    """The unauthenticated gate must expose a Microsoft sign-in entry-point
    that the operator can use without prior session state.
    """
    html = _read_html()
    assert 'data-testid="login-button"' in html
    assert "/auth/microsoft/start" in html


def test_account_nav_supports_multi_account_selection():
    """Account-scoped flows: each linked account is selectable in the nav,
    and selecting one re-scopes dashboard / activity / alerts / sorting.
    """
    html = _read_html()
    assert 'data-testid="account-nav"' in html
    assert 'data-testid="account-nav-list"' in html
    assert "Select one mailbox here" in html
    assert "health-dot" in html
    assert "automation_health" in html
    assert "function selectAccount(email)" in html
    # Sorting log + dashboard reload on account switch.
    assert "loadSortingLog().catch" in html


def test_left_rail_hosts_console_and_session_controls():
    html = _read_html()
    assert 'data-testid="left-service-nav"' in html
    assert "Audit Log" in html
    assert 'data-testid="rail-connect-account"' in html
    assert 'data-testid="rail-sign-out"' in html
    assert "railSignOutButton.addEventListener" in html


def test_activity_log_renders_message_move_events():
    html = _read_html()
    assert "Verbose operational trail" in html
    assert 'id="messageMovementList"' in html
    assert "message_movement" in html
    assert "Moved to" in html


def test_connect_copy_does_not_claim_single_tenant_dhw_blocker():
    html = _read_html()
    assert "currently single-tenant" not in html
    assert "DHW tenant cannot sign in directly" not in html
    assert "Multi-account session" in html


def test_inbox_sorting_string_present():
    """Required by the stabilization checklist: 'Inbox Sorting' must remain
    in the shipped web build.
    """
    html = _read_html()
    assert "Inbox Sorting" in html


def test_dryrun_control_lives_under_diagnostics():
    html = _read_html()
    assert "<strong>Dry-run classify</strong>" in html
    assert "/mail/inbox/classify-dryrun" in html


def test_sorting_panel_exposes_move_action_with_confirmation():
    """The dry-run row must expose a Move action that calls the new
    /mail/inbox/move endpoint with a confirmation prompt before
    issuing the Graph write. The button is rendered per-row by the JS
    table renderer so the static HTML carries the testid attribute and
    the JS path string.
    """
    html = _read_html()
    # Banner/copy explaining what Move does (and what it doesn't do).
    assert 'data-testid="sorting-move-banner"' in html
    assert "Move action" in html
    assert "Nothing is deleted" in html
    # Per-row Move button rendered by the JS table renderer.
    assert 'data-testid="sorting-move-button"' in html
    # The new POST endpoint is wired up.
    assert "/mail/inbox/move" in html
    # A confirmation dialog must run before the Graph call so the
    # operator can't fat-finger a move.
    assert "window.confirm" in html
    # The Action column header must be present so operators can see
    # which column carries the move control.
    assert "<th>Action</th>" in html


def test_safe_automation_control_lives_under_diagnostics():
    html = _read_html()
    assert 'id="diagnosticAutomationButton"' in html
    assert "<strong>Manual automation</strong>" in html
    assert "/mail/inbox/automove" in html
    assert "window.confirm" in html


def test_sorting_panel_shows_automation_active_state():
    html = _read_html()
    assert 'data-testid="sorting-automation-state"' in html
    assert "Automation active" in html
    assert "about every\n                          six minutes" in html
