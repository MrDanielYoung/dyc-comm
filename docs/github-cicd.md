# GitHub CI/CD Setup

## Purpose

This document defines the production-aligned CI/CD shape for DYC Comm.

The goal is to avoid a fragile "temporary pipeline" that has to be replaced later. GitHub Actions should build images, push them to Azure Container Registry, and deploy updates directly to the existing Azure Container Apps.

## Repository Workflows

This repository now includes:

* `.github/workflows/ci.yml`
* `.github/workflows/deploy-web.yml`
* `.github/workflows/deploy-api.yml`

### CI

`ci.yml` runs on push and pull request.

Current checks:

* compile the Python API
* build the web container image
* build the API container image

### CD

`deploy-web.yml` deploys:

* `apps/web`
* Azure Container App: `dyc-comm-prod-web`

`deploy-api.yml` deploys:

* `apps/api`
* Azure Container App: `dyc-comm-prod-api`

Both workflows:

* authenticate to Azure with OIDC through `azure/login`
* build images from repo Dockerfiles
* push images to `dyccommprodacr`
* update the target Azure Container App with the new image

## Azure Identity For GitHub Actions

Do not reuse the Microsoft Graph end-user application registration for CI/CD.

Create a separate Microsoft Entra app registration for GitHub deployments, for example:

* `dyc-comm-github-deploy`

Add a federated credential for the GitHub repository:

* repository: `MrDanielYoung/dyc-comm`
* branch: `refs/heads/main`

If you want deployments from pull requests or another branch later, add separate federated credentials deliberately.

## Azure Roles Required For GitHub Deployments

The GitHub deployment identity should have only the access needed to build and deploy.

Recommended assignments:

* `AcrPush` on `dyccommprodacr`
* `Contributor` on `dyc-comm-prod-rg`

If you want tighter scoping later, split the resource-group rights more narrowly, but `Contributor` at the resource-group level is the practical starting point for Container App updates.

## GitHub Secrets

Add these repository secrets:

* `AZURE_CLIENT_ID`
* `AZURE_TENANT_ID`
* `AZURE_SUBSCRIPTION_ID`

These correspond to the GitHub deployment identity, not the Microsoft Graph user-login app.

## GitHub Repository Settings

Recommended repository controls:

* protect `main`
* require pull requests before merge
* require `CI` status checks before merge
* optionally require review before running production deployments

Recommended GitHub environment:

* `production`

Attach both deploy workflows to the `production` environment if you want approval gates or environment-scoped secrets later.

## Azure Resources Used By Deploy Workflows

Current workflow assumptions:

* Resource group: `dyc-comm-prod-rg`
* ACR: `dyccommprodacr`
* ACR login server: `dyccommprodacr-fmf6dchxeca3bsbf.azurecr.io`
* Web app: `dyc-comm-prod-web`
* API app: `dyc-comm-prod-api`

## Runtime Configuration Still Needed

The workflows will deploy images, but the API container app still needs real runtime configuration in Azure for:

* `DATABASE_URL`
* `MICROSOFT_ENTRA_CLIENT_ID`
* `MICROSOFT_ENTRA_TENANT_ID`
* `MICROSOFT_ENTRA_CLIENT_SECRET`
* `MICROSOFT_ENTRA_REDIRECT_URI`

Prefer Key Vault references once the app config is wired.

## Verification Checklist

After configuring GitHub OIDC and secrets:

1. run `CI` on a pull request
2. merge a harmless change to `main`
3. confirm `Deploy Web` and `Deploy API` both succeed
4. verify the Azure Container Apps show new revisions
5. verify `https://api.comm.danielyoung.io/health` responds successfully once routing is complete
