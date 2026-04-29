# AI Classifier Policy

This document describes the dry-run AI classification slice in DYC Comm:
the provider choice, the safety contract, the data-minimization posture,
the BIOTRONIK disclosure/audit posture, and the human-in-the-loop
controls that gate any future autonomous mailbox action.

It is the canonical reference for `apps/api/app/classifier.py` and the
`POST /classify/recommend` endpoint added in
[apps/api/app/main.py](../apps/api/app/main.py). It complements the
"AI Routing And Safety Policy" section in
[docs/mvp-account-strategy.md](mvp-account-strategy.md), and the
classifier rules in [architecture.md](../architecture.md) §§9–11.

## 1. Scope

The current slice provides:

* a deterministic, dry-run classifier that returns a recommendation
  (`category`, `recommended_folder`, `confidence`, `reasons`,
  `safety_flags`) for one message at a time
* an HTTP entry point at `POST /classify/recommend` that surfaces the
  recommendation only — it does not move, label, send, or delete mail
* configuration scaffolding for Azure OpenAI and Azure AI Foundry,
  reported via `GET /config-check` as presence-only flags

The slice does not provide:

* a real call to any LLM provider
* any mailbox-changing action
* any background worker
* any UI

Future workers may swap a real provider call into the same contract.
They are not permitted to relax any of the safety rules in §3.

## 2. Provider Choice — Azure OpenAI / Azure AI

We use **Azure OpenAI** as the primary provider, with **Azure AI
Foundry** as the secondary option. This choice is driven by:

* **Enterprise / compliance alignment.** Microsoft 365 is the system of
  record for the inboxes we triage. Azure OpenAI processes content under
  Microsoft's enterprise data-protection terms, keeps data within the
  selected Azure region, and does not use customer prompts/completions
  to train foundation models.
* **Tenant boundary.** Authentication, mail access, and AI inference all
  live in the same Microsoft tenant. There is no third-party data
  processor in the path between the mailbox and the model.
* **Operational fit.** The rest of the platform is Azure-native (see
  `architecture.md` §5: Container Apps, Postgres Flexible Server, Key
  Vault, Application Insights). Co-locating the AI provider keeps
  network paths inside Azure and lets us use Managed Identity / Key
  Vault refs for credentials.
* **Identity options.** Azure OpenAI supports both API-key auth (for
  early development) and Microsoft Entra ID via Managed Identity (for
  production), so we can rotate off keys without changing the
  application contract.

### Configuration contract

The following environment variables are reported by `/config-check` as
presence-only flags. Values are never returned in any API response. All
of these variables are **optional** — the deterministic classifier
runs without any of them set.

| Variable | Secret | Purpose |
| --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | no | Azure OpenAI resource endpoint (e.g. `https://<name>.openai.azure.com`) |
| `AZURE_OPENAI_DEPLOYMENT` | no | Deployment name for the chat-completion model |
| `AZURE_OPENAI_API_VERSION` | no | API version pinned for stable contract |
| `AZURE_OPENAI_API_KEY` | yes | API key, when not using Managed Identity |
| `AZURE_AI_ENDPOINT` | no | Azure AI Foundry endpoint (fallback / second model) |
| `AZURE_AI_DEPLOYMENT` | no | Foundry deployment name |
| `AZURE_AI_API_KEY` | yes | Foundry API key |

Production deployments source the secret values from Azure Key Vault
(see [docs/azure-runtime-config.md](azure-runtime-config.md)). They are
never stored in GitHub Secrets, never echoed in logs, and never returned
by any API endpoint.

## 3. Safety Contract

The classifier guarantees the following, by construction:

1. **`10 - Review` is the canonical fallback folder.** Any uncertain or
   sensitive message is recommended for `10 - Review`, never guessed
   into a more specific numbered folder.
2. **Forced-review categories.** Independent of any model confidence
   signal, the following always route to `10 - Review`:
   * sensitive customer- or patient-adjacent content (including the
     keyword `BIOTRONIK` and related implant/clinical terms — see §5)
   * legal or contractual mail that is not unambiguously a sign-here
     document for `70 - Contracts`
   * short messages without enough body to classify confidently
   * thread replies that contain a correction/retraction phrase
     (the newest message changes the meaning of earlier messages)
   * messages whose surface vocabulary requires judgment about tone,
     politics, or obligations
3. **Low-confidence routing.** Any decision below `MEDIUM_THRESHOLD`
   (0.70) is forced to `10 - Review`. The classifier never auto-routes
   a message into a specific numbered folder under low confidence.
4. **No destructive recommendations.** The decision contract has no
   field that recommends deletion or sending. The endpoint only ever
   recommends a folder; the deletion / send paths do not exist in v1.
5. **Stable contract.** The keys returned by the endpoint —
   `category`, `recommended_folder`, `confidence`, `confidence_band`,
   `reasons`, `safety_flags`, `forced_review`, `provider_consulted`,
   `provider` — are pinned by tests and may not change without a
   contract version bump.

## 4. Data Minimization

The classifier accepts an already-redacted payload. Callers must:

* **Send the minimum fields needed.** The contract takes `subject`,
  `body`, `sender`, an `is_thread_reply` boolean, and optional
  `rule_category` / `rule_confidence` advisory hints. No mailbox IDs,
  no provider tokens, no recipient lists.
* **Strip identifiers.** Where possible, callers should replace
  personal identifiers (names, email addresses other than the sender
  domain, phone numbers, account numbers) with redaction tokens before
  invoking the classifier.
* **Truncate.** Large bodies should be truncated to the leading portion
  of the message; the classifier does not require the full body to make
  a routing decision.

The classifier itself does not log the content of messages it sees.
When Azure OpenAI is configured, the request payload is sent over TLS to
the Azure-region endpoint configured by the operator, and Azure's
enterprise-data-protection terms apply. The provider returns only a
category, confidence, and reasons; local safety rules still decide
whether to force `10 - Review`.

## 5. BIOTRONIK Disclosure And Audit Posture

`BIOTRONIK` is one of the customer/patient-adjacent senders we expect
to encounter. The policy here is deliberately conservative:

* The literal token `BIOTRONIK` (and related clinical terms — `patient`,
  `implant`, `pacemaker`, `phi`, `clinical`, etc.) is treated as a
  sensitive-content signal in the deterministic safety pass. Any
  message matching such a term is forced to `10 - Review` regardless
  of model confidence.
* There is no autonomous send, autonomous delete, autonomous forward,
  or auto-archive path for messages flagged sensitive. Any action
  requires explicit human approval per the v1 safety rules.
* When a real provider call is added, every classification on a
  sensitive-flagged message will record:
  * the provider that was consulted (`azure_openai` / `azure_ai`)
  * the model deployment
  * the safety flags that fired
  * the final folder recommendation
  * the human user who later approved or rejected the recommendation
* Audit records are immutable (see `architecture.md` §4 —
  `DYC_Comm_Audit`). Disclosure to BIOTRONIK or other parties, when
  required, will be sourced from the audit log only.

## 6. Human-In-The-Loop Controls

The classifier is dry-run and recommendation-only. The runtime gates
that keep the system safe:

* **No autonomous execution path exists.** The endpoint returns a
  recommendation and stops. No worker today reads from this endpoint
  and applies the recommendation to a mailbox.
* **All mailbox-changing actions remain manual** in v1
  (`architecture.md` §9, `docs/mvp-account-strategy.md` §"Safety Rules
  For MVP"). When a future "approve / reject" UI is added, every move
  will be approved per-thread by the user.
* **Confidence thresholds gate UI presentation, not execution.** Even
  a 0.99-confidence recommendation requires human approval before any
  Graph API write is issued.
* **Reversible-only actions.** When automation is added later it will
  be limited to label / move / draft. Delete and send remain out of
  scope for v1.
* **Operator visibility.** `/config-check` exposes whether the AI
  provider is configured. Operators can therefore tell, without
  calling the endpoint, whether the deterministic-only path is in
  effect and whether a real provider would be consulted on the next
  feature slice.

## 7. Endpoint

`POST /classify/recommend`

Request body (all fields optional):

```json
{
  "subject": "Re: project status",
  "body": "Could you take a look at the attached doc and let me know your thoughts?",
  "sender": "alice@example.com",
  "is_thread_reply": false,
  "rule_category": "human_direct",
  "rule_confidence": 0.85
}
```

Response:

```json
{
  "dry_run": true,
  "recommendation": {
    "category": "human_direct",
    "recommended_folder": "10 - Review",
    "confidence": 0.85,
    "confidence_band": "medium",
    "reasons": ["..."],
    "safety_flags": [],
    "forced_review": true,
    "provider_consulted": false,
    "provider": "none"
  },
  "provider": {"selected": "none", "configured": false},
  "policy_version": "v1.0",
  "review_folder": "10 - Review"
}
```

The endpoint is read-only. It is safe to call from automated tests and
from operator tooling; it never causes a mailbox-changing action.
