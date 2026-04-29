# Outlook Labels

DYC uses Outlook categories as an attention layer on top of folders.
Folders answer what kind of message this is; categories answer what Daniel
should pay attention to next.

Recommended category set:

| Category | Outlook color preset | Outlook color | Purpose |
| --- | --- | --- | --- |
| `DYC - P0 Today` | `preset0` | Red | Same-day or urgent attention. |
| `DYC - P1 This Week` | `preset1` | Orange | Important, but not same-day. |
| `DYC - Reply Needed` | `preset7` | Blue | Daniel owes a response. |
| `DYC - Waiting` | `preset3` | Yellow | Daniel is waiting on someone else. |
| `DYC - Read Later` | `preset10` | Steel | Useful reading, not operationally urgent. |
| `DYC - FYI` | `preset12` | Gray | Informational; no action expected. |
| `DYC - Money` | `preset4` | Green | Payments, invoices, banking, reimbursement, procurement. |
| `DYC - Legal Contract` | `preset9` | Cranberry | Legal, contract, signature, terms, or agreement context. |
| `DYC - Customer Patient` | `preset8` | Purple | Sensitive customer, clinical, patient-adjacent, or privacy context. |
| `DYC - Travel Logistics` | `preset5` | Teal | Flights, hotels, rides, reservations, itineraries, logistics. |
| `DYC - Needs Review` | `preset15` | Dark red | Review/ambiguous/safety-held message. |
| `DYC - Automation Moved` | `preset6` | Olive | Message was moved by DYC automation. |

## Current Automation Behavior

The automation applies these category names to messages using Microsoft
Graph's message `categories` property. This uses the existing
`Mail.ReadWrite` delegated permission.

Color setup lives in Outlook's master category list. Creating or editing
category colors through Graph requires `MailboxSettings.ReadWrite`, so the
current implementation does not add that permission automatically during
the active production rollout. Until that scope is added and Daniel
re-consents, colors can be configured manually in Outlook:

1. Open Outlook.
2. Go to Categories / Manage categories.
3. Create the `DYC - ...` categories above.
4. Assign the suggested colors.

Once the extra permission is intentionally added, DYC can bootstrap this
master category list automatically for every connected mailbox.
