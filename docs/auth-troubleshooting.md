# Auth Troubleshooting

This document covers Microsoft Entra (Azure AD) sign-in errors that the
`dyc-comm` console can surface during the OAuth handshake, and the
operator-side fix paths for each.

## AADSTS50020 — User account from another tenant

### Symptom

A user attempts to sign in (for example
`daniel.young@digitalhealthworks.com`) and Microsoft returns an error page
similar to:

> AADSTS50020: User account `daniel.young@digitalhealthworks.com` from
> identity provider `https://sts.windows.net/3dd54b52-c31e-442e-8705-a56b839e59a7/`
> does not exist in tenant `Decoding Options Inc.` and cannot access the
> application `494bed3b-7049-43a5-bd39-e726758052a9` (`dyc-comm-prod-app`)
> in that tenant. The account needs to be added as an external user in the
> tenant first. Sign out and sign in again with a different Microsoft Entra
> user account.

### Root cause

The Entra app registration `dyc-comm-prod-app`
(`494bed3b-7049-43a5-bd39-e726758052a9`) lives in the **Decoding Options
Inc.** tenant and is configured as **single-tenant**. The user's home
tenant is **Digital Health Works**
(`3dd54b52-c31e-442e-8705-a56b839e59a7`), which is a different Entra
tenant. A single-tenant app rejects every principal that is not a member
or guest of its home tenant.

This is an Entra/Azure configuration constraint, not a bug in the web app
or the API. The web shell only forwards the user to the standard
`authorize` endpoint; Microsoft is the component refusing the sign-in.

### Fix paths

There are two supported ways to let the DHW account use this app. Pick one
based on whether the account should also be able to sign into other apps
in the Decoding Options tenant.

#### Option A — Invite the DHW account as an external (guest) user

Best when only this single account, and only this app, needs cross-tenant
access. Lowest blast radius; no consent required from the DHW tenant.

Steps:

1. In the **Decoding Options Inc.** tenant, open
   *Microsoft Entra ID* → *Users* → *New user* → *Invite external user*.
2. Enter `daniel.young@digitalhealthworks.com` and send the invitation.
3. The user accepts the invitation from their DHW mailbox; this creates a
   guest principal in the Decoding Options tenant.
4. (Optional) Assign the guest user to the `dyc-comm-prod-app` enterprise
   application under *Users and groups* if user assignment is required.
5. Re-attempt the sign-in. Microsoft now resolves the user against the
   Decoding Options tenant (where the app lives), so AADSTS50020 no
   longer applies.

Reference: <https://learn.microsoft.com/entra/external-id/b2b-quickstart-add-guest-users-portal>

#### Option B — Make the Entra app multi-tenant

Best when multiple users from DHW (or other tenants) will use the app.
Requires admin consent from each external tenant.

Steps:

1. In the **Decoding Options Inc.** tenant, open
   *Microsoft Entra ID* → *App registrations* → `dyc-comm-prod-app`
   (`494bed3b-7049-43a5-bd39-e726758052a9`) → *Authentication*.
2. Change *Supported account types* to
   **Accounts in any organizational directory (Any Microsoft Entra ID
   tenant — Multitenant)**.
3. Save. Verify the redirect URIs and the API's accepted issuer claim
   still match — multi-tenant tokens carry the **caller's** tenant id in
   the `tid` claim, so any code that pins `tid` to the Decoding Options
   tenant id must be relaxed or made explicit about which tenants are
   allowed.
4. A Global Admin in the **Digital Health Works** tenant must perform
   admin consent for the app, e.g. via
   `https://login.microsoftonline.com/{dhw-tenant-id}/adminconsent?client_id=494bed3b-7049-43a5-bd39-e726758052a9`.
5. Re-attempt sign-in. The DHW user now authenticates against their home
   tenant and Microsoft issues a token for `dyc-comm-prod-app`.

Reference: <https://learn.microsoft.com/entra/identity-platform/howto-convert-app-to-be-multi-tenant>

### Recommendation

For the current MVP topology — one DHW operator, one app — **Option A
(guest invitation)** is the lighter-weight path and does not require
changes to the Entra app registration or the API's token validation.

**Option B (multi-tenant) — Fyxer-style cross-tenant connect.** This is
the right shape if you want the experience external SaaS apps like
Fyxer offer: each user signs in with their own home-tenant Microsoft
account, without the Decoding Options tenant having to invite them as
a guest. The trade-off is that the app registration must be marked
**Accounts in any organizational directory (Any Microsoft Entra ID
tenant — Multitenant)** in *Authentication* → *Supported account types*.
Choose the **organizational directory only** variant (not "+ personal
Microsoft accounts") unless you specifically want consumer Microsoft
accounts to sign in. After flipping the setting, an admin in each
external tenant must perform the one-time admin-consent step (see
Option B steps above).

When the app is multi-tenant, the runtime **must** enforce a strict
app-side allow-list of which tenants and accounts are permitted to
finish the OAuth callback. This repo enforces that allow-list itself
(see "App-side tenant and account allow-list" below); it is not a
property of the Entra app registration.

Move from Option A to Option B only if multiple users from external
tenants need the console or if guest invitations are blocked by tenant
policy.

## App-side tenant and account allow-list

Even with a multi-tenant Entra app, the API refuses to persist a
connected account unless the caller's tenant id and email survive a
server-side allow-list check. This is the same pattern Fyxer-style
apps use to make multi-tenant safe: Microsoft authenticates the user,
the app authorizes the principal.

### Identity attributes used

After the OAuth code-for-token exchange succeeds against Microsoft's
token endpoint, the callback handler reads:

- `tid` from the ID token payload — the tenant id of the principal that
  just signed in. The ID token is decoded without verifying the
  signature; we treat it as an authorization attribute of the same
  principal whose authorization code we just redeemed over a
  confidential-client TLS exchange (client secret + PKCE), not as
  standalone proof of identity. A missing `tid` is a hard failure.
- `mail` / `userPrincipalName` from Microsoft Graph `/me`, falling back
  to ID token `preferred_username` / `upn` / `email` when Graph does not
  return a usable address.

### Enforcement (`apps/api/app/main.py`)

The callback raises HTTP 403 — and persists no token — when:

- `tid` cannot be established from the token response.
- `tid` is not in `ALLOWED_MICROSOFT_TENANT_IDS` (or, when that env var
  is unset, not equal to `MICROSOFT_ENTRA_TENANT_ID`).
- `ALLOWED_ACCOUNT_EMAILS` is set and the resolved email is not in it.

Each deny path emits a structured log line via the `dyc_comm.auth`
logger (`auth.callback.denied reason=...`) so reviews can see who was
turned away without leaking secret material.

### Configuration

Both env vars are non-secret and committed in
`.github/workflows/configure-api-runtime.yml` so the production identity
policy is reviewable. Adding or removing a tenant or account is a
workflow PR, not a portal click.

| Variable | Behaviour when unset | Example |
| --- | --- | --- |
| `ALLOWED_MICROSOFT_TENANT_IDS` | Falls back to `MICROSOFT_ENTRA_TENANT_ID` only. **External tenants are denied by default.** | `99c0f350-71bd-47f9-ab6a-cf10bc76533a,3dd54b52-c31e-442e-8705-a56b839e59a7` |
| `ALLOWED_ACCOUNT_EMAILS` | No per-email allow-list — any user from an allow-listed tenant may sign in. | `daniel@danielyoung.io,daniel.young@digitalhealthworks.com` |

`/config-check` reports the presence and counts of both lists under
`auth_allow_list` (it never returns the values themselves).

When the operator allow-lists more than one tenant, the runtime also
swaps the `/authorize` and `/token` URLs from the home tenant id over
to `/organizations`, which is required for external-tenant sign-in.

### What does *not* fix this

- Re-running the OAuth flow with the same DHW account.
- Clearing browser cookies or using an incognito window.
- Changing redirect URIs.
- Code changes in this repository — the constraint is enforced by Entra,
  not the web shell or the API.

## See also

- `docs/web-access-control.md` — app-level and platform-level gating
  recommendations for the static shell.
- `docs/azure-runtime-config.md` — runtime configuration of the
  Container Apps deployment.
