# Web Access Control

The `dyc-comm-prod-web` static shell is intended for a single authorized
operator. This document describes the in-app gating that ships with the
console and the stronger platform-level controls recommended on top of it.

For sign-in errors (in particular `AADSTS50020` when a Digital Health
Works account is denied because the Entra app is single-tenant), see
[`auth-troubleshooting.md`](auth-troubleshooting.md).

## App-level SSO gating

The single-page UI in `apps/web/index.html` boots into a locked "Sign in
required" state and reveals the mailbox console only after the API confirms
an authenticated Microsoft 365 session.

How it works:

- On load the page calls `GET /auth/session` against the API with
  `credentials: "include"` so the backend session cookie is sent.
- If the response has no `linked_account`, the UI stays on the locked
  sign-in screen. The "Sign in with Microsoft" button drives the existing
  `GET /auth/microsoft/start` PKCE flow.
- If `linked_account` is present but `mailbox_access_ready` is false, the
  UI shows a "re-authentication required" state and refuses to enable
  mailbox operation buttons.
- Only when the API reports both a linked account and mailbox access does
  the dashboard, folder lists, and operation buttons (`Folders`, `Inventory
  Sync`, `Bootstrap`) become visible and enabled.
- 401 responses from any mailbox endpoint re-trigger the session check and
  re-gate the UI.

The backend already enforces the same rule independently: every
`/mail/folders*` endpoint requires the `dyc_account_email` cookie and
returns 401 otherwise. The UI gating is a usability layer on top of that
enforcement, not a replacement for it. See
`tests/api/test_main.py::test_protected_mailbox_endpoints_reject_unauthenticated_calls`.

## Limitations

App-level gating does not stop an unauthenticated visitor from:

- loading `index.html` and the static assets (`/assets/*`)
- inspecting the bundled JavaScript and inferred API base URL
- discovering the public `/auth/microsoft/start` and `/health` endpoints

It only ensures that no mailbox data or operational controls are shown or
made callable from the page until the API confirms a valid session. This is
sufficient when the goal is "no operational surface for unauthenticated
visitors". It is not sufficient when the goal is "the site must not be
reachable by unauthenticated visitors at all".

## Recommended platform-level controls

If the requirement is to prevent unauthenticated callers from loading the
static shell at all, layer one or both of the following on top of the
app-level gating.

### Option A — Azure Container Apps built-in authentication (EasyAuth)

Enable Container Apps' built-in authentication on `dyc-comm-prod-web` with
the Microsoft identity provider, and configure unauthenticated requests to
be redirected to the provider sign-in. This blocks anonymous requests at
the ingress before any static asset is served.

Reference: <https://learn.microsoft.com/azure/container-apps/authentication>

Concretely:

1. Add a Microsoft identity provider to the web container app and set
   "Restrict access" to "Require authentication" with "Redirect to
   Microsoft" as the unauthenticated action.
2. Reuse the existing Entra app registration's tenant and configure the
   allowed audiences to the web app's hostname.
3. Leave the API container app with its current OAuth flow — EasyAuth on
   the web shell does not interfere with the cross-origin
   `/auth/microsoft/start` handshake to the API.

EasyAuth on the web shell is the lowest-effort way to make the site
non-public.

### Option B — Container Apps ingress IP allow-list

If the operator works from a small set of fixed egress IPs, add an
ingress IP security restriction on `dyc-comm-prod-web` (and optionally on
the API) to drop everything else at the platform.

Reference: <https://learn.microsoft.com/azure/container-apps/ip-restrictions>

This is appropriate when the operator's network IPs are stable, and it
composes well with EasyAuth.

### Option C — Front Door / WAF

If the deployment moves behind Azure Front Door, attach a WAF policy with
a Microsoft Entra-backed access rule or geo/IP restrictions. This is
overkill for the current MVP topology and is listed for completeness.

## Recommendation

Enable **Option A** (Container Apps built-in Microsoft authentication on
`dyc-comm-prod-web`) as the platform-level control. It matches the "tight
Microsoft SSO" requirement, requires no code changes, and composes with
the in-app gating already shipping in this repo.
