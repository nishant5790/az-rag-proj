# Production DevOps Handoff for Azure Multimodal RAG

## Purpose

This document is a repo-specific production handoff for DevOps based on the current codebase under `azure-mmr/`, `k8s/`, and `infra/`.

It is written for the case where application development is done by the project team, but Azure resource creation, platform setup, DNS, certificates, identities, container registry, cluster add-ons, and deployment automation must be handled by DevOps.

This is not a generic Azure checklist. Every item below comes from what the code already expects at runtime or during setup.

---

## Executive Summary

The application is a two-service containerized workload:

1. A FastAPI backend on port `8000`.
2. A Streamlit frontend on port `8501`.

The backend depends on these Azure services:

1. Azure AI Search.
2. Azure Blob Storage.
3. Azure OpenAI.
4. Azure Document Intelligence.
5. Azure Key Vault.

The repo already assumes the following production platform model:

1. AKS.
2. Azure Workload Identity.
3. Azure Container Registry.
4. Key Vault CSI driver.
5. NGINX Ingress.
6. cert-manager.
7. Public DNS records for frontend and API.

This handoff assumes the application team cannot create Azure services directly. DevOps must therefore create the required Azure services and platform components, not just provide access to pre-existing ones.

Important: the repo contains bootstrap shell scripts and Kubernetes YAMLs, but it does not contain full production Infrastructure as Code for all Azure dependencies. In particular, it does not provision Azure AI Search, Storage, Azure OpenAI, or Document Intelligence resources.

Important: the repo currently contains plaintext Azure secrets in checked-in `.env` files, including backup copies. Those credentials must be treated as compromised and rotated before production.

---

## Current Architecture in This Repo

### Application services

1. `azure-mmr/api/main.py`
   Exposes:
   - `POST /search`
   - `GET /blob-proxy/{blob_name}`
   - `GET /pipeline/status`
   - `POST /pipeline/setup`
   - `POST /pipeline/teardown`
   - `GET /healthz`

2. `azure-mmr/app.py`
   Streamlit frontend that calls the FastAPI backend through `FASTAPI_URL`.

### Containerization

1. `azure-mmr/Dockerfile.api`
   - Base image: `python:3.11-slim`
   - Exposes port `8000`
   - Health check: `GET /healthz`
   - Uvicorn workers: `2`

2. `azure-mmr/Dockerfile.streamlit`
   - Base image: `python:3.11-slim`
   - Exposes port `8501`
   - Health check: `GET /_stcore/health`

### Kubernetes assumptions already present

1. Namespace `mmr`.
2. ServiceAccount `mmr-sa` with Workload Identity client ID annotation.
3. SecretProviderClass using Azure Key Vault Provider for Secrets Store CSI Driver.
4. `mmr-api` Deployment and Service.
5. `mmr-streamlit` Deployment and Service.
6. Ingress for:
   - `mmr.company.com`
   - `api.mmr.company.com`
7. HPA for the API deployment.

### Azure setup logic already present in the code

The backend and CLI can create or update the following Azure AI Search objects:

1. Data source.
2. Search index.
3. Skillset.
4. Indexer.

Those operations require Azure-side permissions and working connectivity.

---

## What DevOps Must Provision

This section is the exact infrastructure ask.

### 1. Azure Kubernetes Service

Ask DevOps to provision an AKS cluster with all of the following enabled:

1. OIDC issuer enabled.
2. Azure Workload Identity enabled.
3. Monitoring enabled.
4. At least one user node pool suitable for application workloads.
5. Outbound HTTPS access from pods to Azure AI Search, Storage, Azure OpenAI, Document Intelligence, Key Vault, package repositories, and certificate endpoints.

Minimum expected outcome:

1. Cluster reachable by DevOps deployment automation.
2. Namespace `mmr` created.
3. Workload Identity usable by pods in namespace `mmr`.

Recommended production ask:

1. Separate system and user node pools.
2. Cluster autoscaler enabled.
3. Container Insights or Azure Monitor for containers connected to a Log Analytics workspace.
4. Network policy enabled if required by platform standards.

### 2. Azure Container Registry

Ask DevOps to provision one ACR and grant the AKS cluster permission to pull from it.

Required images:

1. `mmr-api`
2. `mmr-streamlit`

Required build inputs:

```bash
docker build -f azure-mmr/Dockerfile.api -t <acr>.azurecr.io/mmr-api:<tag> azure-mmr
docker build -f azure-mmr/Dockerfile.streamlit -t <acr>.azurecr.io/mmr-streamlit:<tag> azure-mmr
docker push <acr>.azurecr.io/mmr-api:<tag>
docker push <acr>.azurecr.io/mmr-streamlit:<tag>
```

Ask DevOps to avoid `latest` in production and use immutable image tags.

### 3. Azure Key Vault

Ask DevOps to provision a Key Vault with RBAC authorization enabled.

It must be used for runtime configuration injection into Kubernetes through the Key Vault CSI provider.

At minimum, Key Vault must hold the configuration values listed later in this document under `Required Runtime Configuration`.

### 4. Azure AI Search

Ask DevOps to provision an Azure AI Search service suitable for:

1. Index creation.
2. Indexer execution.
3. Skillset execution.
4. Semantic search configuration.
5. Vector field support.

Minimum recommendation:

1. Do not use Free.
2. Use at least a production-capable SKU such as Standard tier after DevOps validates indexing scale, semantic search needs, and expected query volume.

DevOps must also ensure the Search service has a system-assigned managed identity enabled if production will use managed identity rather than admin keys.

### 5. Azure Storage Account

Ask DevOps to provision a StorageV2 account with a blob container for source documents.

The code expects:

1. Storage account name.
2. Blob container name.
3. Backend blob reads through managed identity in production.
4. Search indexer reads from the storage account through either SAS or ResourceId-based connection.

Production target should be managed identity, not SAS.

### 6. Azure OpenAI

Ask DevOps to create an Azure OpenAI resource for this application, or create the required deployment in an existing enterprise-managed Azure OpenAI resource if shared services are mandated by policy.

Minimum required outcome:

1. A usable Azure OpenAI endpoint owned or managed by DevOps.
2. An embedding deployment created by DevOps for this application.
3. Role assignments completed so the production auth model works without developer-managed keys.

The code expects:

1. Azure OpenAI endpoint.
2. Embedding deployment name.
3. Embedding model currently aligned to `text-embedding-ada-002` and `1536` dimensions.

DevOps must confirm that the deployed model and deployment name match the app configuration exactly.

### 7. Azure Document Intelligence

Ask DevOps to create an Azure Document Intelligence resource for this application, or create and assign access through an existing enterprise-managed Document Intelligence service if shared services are mandated by policy.

Minimum required outcome:

1. A usable Document Intelligence endpoint managed by DevOps.
2. The production authentication path validated before release.

The code expects:

1. Document Intelligence endpoint.
2. Potentially keyless production access through managed identity or Search skill auth.

Important gap to review:

The current `setup/skillset.py` does not explicitly pass a Document Intelligence endpoint, key, or identity block in the skillset body. DevOps and the application team must confirm how `DocumentIntelligenceLayoutSkill` will authenticate in the target Azure Search setup. This is a production blocker until clarified.

### 8. DNS and TLS

Ask DevOps to provide:

1. Public DNS records for the frontend hostname.
2. Public DNS records for the API hostname.
3. TLS certificates for both hostnames.
4. Either cert-manager automation or enterprise certificate integration.

The current manifests assume:

1. `mmr.company.com`
2. `api.mmr.company.com`

Replace those placeholders with real corporate domains.

### 9. Ingress Controller

Ask DevOps to install and manage an ingress controller compatible with the existing manifests.

The repo currently assumes NGINX Ingress and WebSocket support for Streamlit.

### 10. Secrets Store CSI Driver and Azure Key Vault Provider

Ask DevOps to install:

1. Secrets Store CSI Driver.
2. Azure Key Vault provider.
3. Secret sync enabled, because the manifests expect `secretObjects` to materialize a Kubernetes secret named `mmr-secrets`.

### 11. Optional Azure API Management

The repo includes an `infra/apim.sh` script. If APIM is required by enterprise standards, ask DevOps to provision it.

If APIM is used, ask DevOps to:

1. Import the FastAPI OpenAPI spec.
2. Enforce authentication.
3. Enforce rate limiting.
4. Restrict CORS.
5. Expose only approved routes.

Note: the sample script uses `Developer` SKU, which is not a production SKU decision. DevOps must choose the enterprise-approved APIM tier.

---

## Required Runtime Configuration

These are the concrete settings the code expects.

### Required by the FastAPI backend

1. `AZURE_SEARCH_SERVICE_ENDPOINT`
2. `AZURE_SEARCH_INDEX_NAME`
3. `AZURE_STORAGE_ACCOUNT_NAME`
4. `AZURE_BLOB_CONTAINER_NAME`
5. `AZURE_OPENAI_ENDPOINT`
6. `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`
7. `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`
8. `AZURE_SUBSCRIPTION_ID`
9. `AZURE_RESOURCE_GROUP`
10. `ALLOWED_ORIGINS`

### Optional for local development fallback only, not recommended for production

1. `AZURE_SEARCH_ADMIN_KEY`
2. `AZURE_BLOB_SAS_TOKEN`
3. `BLOB_SAS_URL`
4. `AZURE_OPENAI_KEY`
5. `AZURE_DOCUMENT_INTELLIGENCE_KEY`

### Required by Streamlit

1. `FASTAPI_URL`
2. `AZURE_SEARCH_INDEX_NAME`

### Required Key Vault secret names implied by the existing Kubernetes SecretProviderClass

The current `k8s/secret-provider-class.yaml` expects these object names in Key Vault:

1. `AZURE-SEARCH-SERVICE-ENDPOINT`
2. `AZURE-SEARCH-INDEX-NAME`
3. `AZURE-STORAGE-ACCOUNT-NAME`
4. `AZURE-BLOB-CONTAINER-NAME`
5. `AZURE-OPENAI-ENDPOINT`
6. `AZURE-OPENAI-EMBEDDING-DEPLOYMENT`
7. `AZURE-DOCUMENT-INTELLIGENCE-ENDPOINT`
8. `AZURE-SUBSCRIPTION-ID`
9. `AZURE-RESOURCE-GROUP`

Ask DevOps to confirm whether they want to keep these exact secret names or change both the Key Vault objects and Kubernetes manifest together.

### Recommendation on secret classification

These values can be handled as secrets in Key Vault even if some are technically configuration and not credentials:

1. Endpoints.
2. Resource names.
3. Index name.
4. Subscription and resource group identifiers.

This is consistent with the current manifest pattern and reduces divergence.

---

## Identity and RBAC Requirements

### A. User-assigned managed identity for the application pods

Ask DevOps to create a user-assigned managed identity and federate it to the Kubernetes service account used by the API deployment.

The service account in the repo is:

1. Namespace: `mmr`
2. ServiceAccount: `mmr-sa`

The existing manifests expect this identity client ID to be injected into:

1. `k8s/service-account.yaml`
2. `k8s/secret-provider-class.yaml`

### B. Role assignments for the workload identity used by the API pod

Ask DevOps to grant the application managed identity these roles:

1. `Search Service Contributor` on the Azure AI Search service.
2. `Search Index Data Contributor` on the Azure AI Search service.
3. `Storage Blob Data Reader` on the Storage account.
4. `Key Vault Secrets User` on the Key Vault.

Additional role to discuss:

1. `Cognitive Services User` on the Document Intelligence resource if the application itself will ever call it directly in production.

### C. Role assignments for the Azure AI Search service identity

Ask DevOps to enable the Search service identity and grant it at least:

1. `Storage Blob Data Reader` on the Storage account, because the indexer reads source documents from blob storage.
2. `Cognitive Services OpenAI User` on the Azure OpenAI resource, because the embedding skill is intended to run without an API key in production.

Potential additional role to validate:

1. Appropriate access for Document Intelligence if the layout skill requires Search-to-Document-Intelligence authorization in your chosen setup.

### D. AKS to ACR pull access

Ask DevOps to grant the AKS cluster pull access to ACR, either by cluster attach or RBAC.

---

## Networking and Security Ask

### Ingress and hostnames

Ask DevOps to expose:

1. Frontend on HTTPS.
2. API on HTTPS.
3. Only approved origins in backend CORS.

Current code behavior:

1. The API reads `ALLOWED_ORIGINS`.
2. If unset, it falls back to `*`.

Production ask:

1. Do not allow wildcard CORS.
2. Set `ALLOWED_ORIGINS` to the approved frontend host only.

### API protection

Important: the backend currently exposes administrative routes:

1. `POST /pipeline/setup`
2. `POST /pipeline/teardown`
3. `GET /pipeline/status`

There is no authentication or authorization implemented in FastAPI itself.

Ask DevOps to ensure one of these is done before production go-live:

1. Block those endpoints entirely at ingress or APIM.
2. Expose them only on an internal admin path.
3. Require enterprise authentication and authorization in front of them.

If nothing else is possible, ask DevOps not to publish those routes externally.

### TLS trust and corporate CA handling

The repo already contains a runtime SSL investigation report showing certificate trust failures inside Docker when calling Azure services from the container.

Ask DevOps to confirm how outbound certificate trust is handled in the target runtime. If the corporate network uses TLS interception, they must bake the internal root CA into the container image or otherwise ensure the Linux trust store trusts the outbound chain.

Example Dockerfile fragment if DevOps requires enterprise CA injection:

```dockerfile
COPY corp-root-ca.crt /usr/local/share/ca-certificates/corp-root-ca.crt
RUN update-ca-certificates
```

This must be handled by DevOps if the production network path requires it.

### Key handling policy

Ask DevOps to enforce the following production rules:

1. No admin keys in Kubernetes manifests.
2. No SAS tokens in production unless there is no managed-identity option.
3. No checked-in `.env` with real values.
4. All runtime values sourced from Key Vault or approved platform configuration.

---

## Deployment Ask

Since the development team is not allowed to create repos or CI/CD, ask DevOps to provide one of these approved release mechanisms:

1. An existing enterprise pipeline that builds images, pushes to ACR, and deploys Kubernetes manifests.
2. A manually triggered release runbook owned by DevOps.
3. A shared platform pipeline where the app team only supplies image tags and manifest changes.

### Minimum deployment workflow DevOps should implement

1. Build `mmr-api` image.
2. Build `mmr-streamlit` image.
3. Push both images to ACR.
4. Replace image references in the manifests with immutable tags.
5. Apply Kubernetes resources in a controlled order.
6. Verify health endpoints.
7. Verify DNS and TLS.
8. Verify blob proxy and search behavior from the frontend.

### Recommended manifest application order

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/service-account.yaml
kubectl apply -f k8s/secret-provider-class.yaml
kubectl apply -f k8s/api-deployment.yaml
kubectl apply -f k8s/api-service.yaml
kubectl apply -f k8s/streamlit-deployment.yaml
kubectl apply -f k8s/streamlit-service.yaml
kubectl apply -f k8s/hpa.yaml
kubectl apply -f k8s/ingress.yaml
```

### Required placeholder replacements before deployment

DevOps must replace these placeholders in the current manifests:

1. `<ACR_NAME>`
2. `<MI_CLIENT_ID>`
3. `<KV_NAME>`
4. `<TENANT_ID>`
5. `mmr.company.com`
6. `api.mmr.company.com`

---

## Monitoring, Logging, and Alerts Ask

Ask DevOps to wire the application into the standard enterprise observability stack.

### Minimum logs and metrics to collect

1. Container stdout and stderr from both deployments.
2. Pod restart count.
3. Ingress request volume and ingress error rate.
4. HTTP `5xx` rate for the API.
5. AKS node and pod CPU and memory metrics.
6. HPA events.
7. Certificate renewal status if cert-manager is used.

### Minimum alerts to request

1. API health endpoint failing.
2. Pod crash loop.
3. Elevated API `5xx` rate.
4. Ingress backend unavailable.
5. TLS certificate expiry.
6. Search indexer failures if setup is executed in production.

### Operational dashboards DevOps should provide

1. Frontend availability dashboard.
2. API availability and latency dashboard.
3. AKS pod and node health dashboard.
4. Certificate expiration dashboard.
5. Search service and storage connectivity error dashboard if platform supports it.

---

## Data and Search Setup Ask

The code can create the search data source, index, skillset, and indexer, but production ownership must be agreed.

Ask DevOps and platform owners to decide which of these models will be used:

### Option 1. Application-owned search setup

The API or CLI runs `setup()` and creates Search artifacts dynamically.

Required conditions:

1. API workload identity must have contributor-level Search permissions.
2. Admin routes must be protected.
3. Platform team must allow runtime infrastructure mutation.

### Option 2. DevOps-owned search setup

DevOps or platform automation creates the Search artifacts during deployment.

Required conditions:

1. A deployment-time job, script, or automation must run `azure-mmr/main.py setup` or equivalent.
2. The runtime API can be given reduced permissions later.

### Recommendation

For production, ask DevOps to own the initial provisioning of Search artifacts and avoid exposing `setup` and `teardown` publicly.

---

## Production Gaps and Risks Found in This Codebase

These are specific findings from the repo that DevOps should know before attempting a production rollout.

### 1. Real Azure secrets are checked into the repo

Observed in checked-in `.env` files under:

1. `azure-mmr/.env`
2. `playground/backup/azure-mmr/.env`

Impact:

1. Current credentials must be assumed compromised.
2. The same values may already exist in shell history, logs, container builds, or backups.

Ask DevOps and security to do this immediately:

1. Rotate Search admin key.
2. Rotate Azure OpenAI key.
3. Rotate Document Intelligence key.
4. Revoke and recreate any SAS tokens.
5. Replace all runtime use of those values with Key Vault or managed identity.

### 2. Administrative pipeline routes are not protected

Impact:

1. Anyone reaching those endpoints could trigger setup, teardown, or status calls.

Ask DevOps to block or protect them before go-live.

### 3. Document Intelligence integration is not fully explicit in the skillset definition

Impact:

1. Production deployment may fail or behave differently depending on the Azure Search skill authentication model.

Ask DevOps and the application team to validate this in a non-production environment before production rollout.

### 4. Current repo scripts do not provision all required Azure services

The existing `infra/` scripts provision these platform components:

1. Resource group.
2. ACR.
3. Key Vault.
4. AKS.
5. NGINX ingress.
6. cert-manager.
7. Key Vault CSI driver.

They do not provision:

1. Azure AI Search.
2. Storage account and blob container.
3. Azure OpenAI resource and deployment.
4. Document Intelligence resource.

Those must be created and provisioned separately by DevOps because the application team cannot create Azure services directly.

### 5. APIM sample is not production-ready by default

The script uses a Developer-tier example and placeholder policy values.

Ask DevOps to treat it as a starting point only.

### 6. README and runtime location for `.env` are inconsistent

The README says `.env` is one level above `azure-mmr/`, while local compose usage reads `.env` from inside `azure-mmr/`.

Impact:

1. Manual deployment can easily inject the wrong file or no file.

This matters less once production uses Key Vault and Kubernetes-managed configuration, but DevOps should know local documentation is not fully aligned.

### 7. Search runtime is currently BM25 only

The index supports vector and semantic configuration, but `search/client.py` currently executes standard full-text search only.

Impact:

1. Production users may expect hybrid or semantic ranking that is not yet wired into the runtime query path.

This is not a DevOps blocker, but it affects production expectations.

---

## Production Readiness Decisions DevOps Must Confirm

Ask DevOps to explicitly confirm these decisions in writing:

1. AKS is the approved runtime for this app.
2. ACR is the approved image registry for this app.
3. Workload Identity is the approved production auth model.
4. Key Vault CSI is the approved secret delivery method.
5. Whether APIM is mandatory or optional.
6. Whether public ingress is allowed or private ingress is required.
7. Whether outbound traffic goes through TLS interception and requires enterprise CA injection.
8. Which team owns Azure AI Search artifact creation.
9. Which team owns blob ingestion and index refresh operations.
10. Whether the admin setup and teardown endpoints will be disabled, internalized, or fronted by enterprise auth.

---

## Exact Ask to DevOps

The fastest way to use this document is to send DevOps the request below.

### Copy/paste request

Please provision and operate the production platform for the Azure Multimodal RAG application in this repo with the following requirements:

1. Create an AKS environment with OIDC issuer, Azure Workload Identity, monitoring, and outbound access to Azure AI Search, Storage, Azure OpenAI, Document Intelligence, and Key Vault.
2. Create an ACR and support deployment of two images: `mmr-api` and `mmr-streamlit`, using immutable image tags.
3. Create a Key Vault and wire it to AKS through Secrets Store CSI Driver with Azure Key Vault provider and secret sync enabled.
4. Create the required Azure services for this application because the app team cannot create Azure services directly. This includes Azure AI Search, Azure Blob Storage, Azure OpenAI, and Azure Document Intelligence. If enterprise policy requires shared services instead of dedicated ones, please create the required app-specific deployments, containers, identities, access scopes, and configuration on those shared services.
5. Create a user-assigned managed identity for the API workload and federate it to Kubernetes ServiceAccount `mmr/mmr-sa`.
6. Grant the application managed identity `Search Service Contributor`, `Search Index Data Contributor`, `Storage Blob Data Reader`, and `Key Vault Secrets User` at the correct scopes.
7. Enable the Azure AI Search service identity and grant it `Storage Blob Data Reader` on the Storage account and `Cognitive Services OpenAI User` on the Azure OpenAI resource. Also confirm whether Document Intelligence access is required for the Search service identity.
8. Replace all placeholders in `k8s/` manifests, including ACR name, managed identity client ID, Key Vault name, tenant ID, and production hostnames.
9. Provide HTTPS ingress for the frontend and API, including DNS and TLS certificate management.
10. Ensure backend CORS is restricted to the production frontend origin only.
11. Do not expose `/pipeline/setup` and `/pipeline/teardown` publicly unless protected by enterprise authentication and authorization.
12. Provide an approved release mechanism because the app team cannot create repos or CI/CD. The release mechanism must build both images, push to ACR, deploy Kubernetes manifests, and validate health endpoints.
13. Provide monitoring, logs, and alerts for API availability, pod health, ingress health, TLS certificate expiry, and deployment failures.
14. Confirm the production solution for container trust store handling if the corporate network performs TLS interception.
15. Treat current checked-in Azure keys and SAS tokens as compromised and rotate them before any production deployment.
16. Confirm in the handoff response which Azure services DevOps will create as dedicated app resources versus which ones will be implemented through existing shared enterprise services.

---

## Validation Checklist for First Production-Like Deployment

Ask DevOps to verify all of the following in a non-production environment before go-live:

1. Both images build successfully without embedding secrets.
2. Both images pull successfully from ACR.
3. API pod starts and returns `200` on `/healthz`.
4. Streamlit pod starts and returns `200` on `/_stcore/health`.
5. Key Vault secrets are mounted and synced into `mmr-secrets`.
6. API can query Azure AI Search using managed identity.
7. API can read blobs through `GET /blob-proxy/{blob_name}` using managed identity.
8. Azure AI Search indexer can read from Blob Storage.
9. Azure AI Search embedding skill can call Azure OpenAI.
10. Document Intelligence layout extraction works in the chosen production auth model.
11. Frontend can call backend through the production hostname without CORS errors.
12. TLS certificates issue and renew correctly.
13. Monitoring and alerts are visible in the standard operations workspace.

---

## Final Recommendation

The codebase is close to a workable AKS-based production shape, but it is not production-ready by configuration alone. DevOps should treat this as an application that already assumes AKS, Workload Identity, Key Vault CSI, ingress, and Azure AI services, while still requiring platform-owned setup for identity, DNS, TLS, ACR, monitoring, and the core Azure data and AI resources.

The three highest-priority items before production are:

1. Rotate all checked-in secrets and remove secret-based production auth wherever possible.
2. Lock down or remove the administrative pipeline endpoints from public exposure.
3. Resolve and validate the Document Intelligence authentication model used by the Search skillset.
