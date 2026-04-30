# Outlook Labels

DYC uses Outlook categories as an attention layer on top of folders.
Folders answer what kind of message this is; categories answer what Daniel
should pay attention to next.

Recommended category set:

| Category | Outlook color preset | Outlook color | Purpose |
| --- | --- | --- | --- |
| `< Today >` | `preset0` | Red | Same-day or urgent attention. |
| `< This Week >` | `preset1` | Orange | Important, but not same-day. |
| `< Reply >` | `preset7` | Blue | Daniel owes a response. |
| `< Waiting >` | `preset3` | Yellow | Daniel is waiting on someone else. |
| `< Read Later >` | `preset10` | Steel | Useful reading, not operationally urgent. |
| `< FYI >` | `preset12` | Gray | Informational; no action expected. |
| `< Money >` | `preset4` | Green | Payments, invoices, banking, reimbursement, procurement. |
| `< Legal >` | `preset9` | Cranberry | Legal, contract, signature, terms, or agreement context. |
| `< Customer >` | `preset8` | Purple | Sensitive customer, clinical, patient-adjacent, or privacy context. |
| `< Travel >` | `preset5` | Teal | Flights, hotels, rides, reservations, itineraries, logistics. |
| `< Review >` | `preset15` | Dark red | Review/ambiguous/safety-held message. |
| `< Moved >` | `preset6` | Olive | Message was moved by DYC automation. |

## Current Automation Behavior

The automation has a guarded code path that can apply these category names
to moved messages using Microsoft Graph's message `categories` property.
This uses the existing `Mail.ReadWrite` delegated permission.

This path is disabled in production unless
`OUTLOOK_CATEGORY_LABELS_ENABLED=true` is set. The first production attempt
showed that category writes need their own verified rollout rather than
being folded into the active backlog cleanup.

Color setup lives in Outlook's master category list. Creating categories
with colors through Graph requires `MailboxSettings.ReadWrite`. The app
requests that scope and exposes a guarded diagnostic endpoint:

```text
POST /mail/categories/bootstrap
```

The endpoint creates missing `< ... >` categories for every visible connected
mailbox that has a usable refresh token. It skips mailboxes that still need
reconnection or tenant repair.

If the endpoint is unavailable or a mailbox has not re-consented to the
new scope, colors can still be configured manually in Outlook:

1. Open Outlook.
2. Go to Categories / Manage categories.
3. Create the `< ... >` categories above.
4. Assign the suggested colors.

BoldWorks currently fails Microsoft sign-in with AADSTS50020, so category
bootstrap cannot touch that mailbox until its tenant/guest access issue is
fixed.
