# Overnight Handoff

## What Was Added Locally

This uncommitted slice adds:

* Microsoft OAuth start/callback/session/logout endpoints in `apps/api/app/main.py`
* a first account-linking web UI in `apps/web/index.html`
* API test scaffolding in `tests/api/test_main.py`
* CI updates to run the API test suite
* repo-native Azure runtime configuration assets:
  * `.github/workflows/configure-api-runtime.yml`
  * `infra/azure/api-runtime-settings.env.example`
  * `infra/azure/apply-api-settings.sh`
* runtime and CI/CD documentation updates
* Docker ignore files for cleaner image contexts

## Local Verification Performed

Verified:

* Python compile check for `apps/api/app`
* shell syntax check for `infra/azure/apply-api-settings.sh`

Not fully verified locally:

* FastAPI runtime tests
* live OAuth redirect flow
* API runtime workflow execution

Reason:

* the local environment does not currently have the FastAPI/pytest dependencies installed outside the container/CI path

## Safe Next Step

1. create a fresh feature branch from the latest `main`
2. commit this auth/runtime-config slice
3. open a PR
4. let CI run the new tests and Docker builds
5. if green, merge and then:
   * run `Configure API Runtime`
   * run `Deploy API`
   * run `Deploy Web`

## Expected Production Secrets For Runtime Workflow

The new `Configure API Runtime` workflow expects these GitHub secrets in addition to the existing Azure and registry secrets:

* `DATABASE_URL`
* `MICROSOFT_ENTRA_CLIENT_ID`
* `MICROSOFT_ENTRA_TENANT_ID`
* `MICROSOFT_ENTRA_CLIENT_SECRET`

## Morning Test Checklist

After the runtime values are applied and the new deploys land:

1. load `https://comm.danielyoung.io`
2. verify the readiness panel on the page
3. start Microsoft sign-in
4. confirm callback returns to the web app
5. verify linked account state appears in the UI
