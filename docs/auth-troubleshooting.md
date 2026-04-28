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
changes to the Entra app registration or the API's token validation. Move
to Option B only if multiple DHW users will need the console or if guest
invitations are blocked by tenant policy.

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
