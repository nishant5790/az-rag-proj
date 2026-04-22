#!/usr/bin/env bash
# infra/aad.sh - Entra ID App Registration, Managed Identity, Workload Identity,
#                and RBAC role assignments.
#
# Prerequisites: provision.sh must have completed successfully.
#
# Required env vars (in addition to those from provision.sh):
#   TENANT_ID       - Azure AD tenant ID
#   SEARCH_RESOURCE - Azure AI Search resource name (e.g. srch-mmr-prod)
#   SEARCH_RG       - Resource group of the Search service
#   OPENAI_RESOURCE - Azure OpenAI resource name
#   OPENAI_RG       - Resource group of the OpenAI resource
#   DOCINTEL_RESOURCE - Document Intelligence resource name
#   DOCINTEL_RG     - Resource group of the Document Intelligence resource
#   STORAGE_ACCOUNT - Storage account name
#   STORAGE_RG      - Resource group of the storage account

set -euo pipefail

SUBSCRIPTION=$(az account show --query id -o tsv)
OIDC_ISSUER=$(az aks show -g "$RG" -n "$CLUSTER_NAME" --query oidcIssuerProfile.issuerUrl -o tsv)

# ── App Registration ──────────────────────────────────────────────────────
echo "==> Creating Entra ID App Registration mmr-app"
APP_ID=$(az ad app create \
  --display-name "mmr-app" \
  --sign-in-audience AzureADMyOrg \
  --query appId -o tsv)

# Expose an API scope
az ad app update --id "$APP_ID" \
  --identifier-uris "api://$APP_ID"

az ad app permission add --id "$APP_ID" \
  --api "$APP_ID" \
  --api-permissions "00000000-0000-0000-0000-000000000000=Scope"

echo "    App ID: $APP_ID"

# ── User-Assigned Managed Identity (Workload Identity) ────────────────────
echo "==> Creating Managed Identity mmr-workload-identity"
MI_CLIENT_ID=$(az identity create \
  --name mmr-workload-identity \
  --resource-group "$RG" \
  --query clientId -o tsv)

MI_OBJECT_ID=$(az identity show \
  --name mmr-workload-identity \
  --resource-group "$RG" \
  --query principalId -o tsv)

echo "    MI client ID: $MI_CLIENT_ID"
echo "    MI object ID: $MI_OBJECT_ID"

# ── Federated credential (AKS ServiceAccount <-> Managed Identity) ────────
echo "==> Creating federated credential"
az identity federated-credential create \
  --name mmr-federated-cred \
  --identity-name mmr-workload-identity \
  --resource-group "$RG" \
  --issuer "$OIDC_ISSUER" \
  --subject "system:serviceaccount:mmr:mmr-sa" \
  --audience api://AzureADTokenExchange

# ── RBAC Role Assignments ─────────────────────────────────────────────────
echo "==> Assigning roles to Managed Identity"

# Azure AI Search
SEARCH_ID=$(az search service show -n "$SEARCH_RESOURCE" -g "$SEARCH_RG" --query id -o tsv)
az role assignment create --assignee "$MI_OBJECT_ID" \
  --role "Search Index Data Contributor" --scope "$SEARCH_ID"
az role assignment create --assignee "$MI_OBJECT_ID" \
  --role "Search Service Contributor" --scope "$SEARCH_ID"

# Azure OpenAI
OPENAI_ID=$(az cognitiveservices account show -n "$OPENAI_RESOURCE" -g "$OPENAI_RG" --query id -o tsv)
az role assignment create --assignee "$MI_OBJECT_ID" \
  --role "Cognitive Services OpenAI User" --scope "$OPENAI_ID"

# Document Intelligence
DOCINTEL_ID=$(az cognitiveservices account show -n "$DOCINTEL_RESOURCE" -g "$DOCINTEL_RG" --query id -o tsv)
az role assignment create --assignee "$MI_OBJECT_ID" \
  --role "Cognitive Services User" --scope "$DOCINTEL_ID"

# Storage (blob data reader)
STORAGE_ID=$(az storage account show -n "$STORAGE_ACCOUNT" -g "$STORAGE_RG" --query id -o tsv)
az role assignment create --assignee "$MI_OBJECT_ID" \
  --role "Storage Blob Data Reader" --scope "$STORAGE_ID"

# Key Vault (secrets reader)
KV_ID=$(az keyvault show -n "$KV_NAME" -g "$RG" --query id -o tsv)
az role assignment create --assignee "$MI_OBJECT_ID" \
  --role "Key Vault Secrets User" --scope "$KV_ID"

# Azure AI Search service also needs Storage Blob Data Reader (for indexer)
SEARCH_MI_OID=$(az search service show -n "$SEARCH_RESOURCE" -g "$SEARCH_RG" \
  --query identity.principalId -o tsv 2>/dev/null || echo "")
if [ -n "$SEARCH_MI_OID" ]; then
  az role assignment create --assignee "$SEARCH_MI_OID" \
    --role "Storage Blob Data Reader" --scope "$STORAGE_ID"
  az role assignment create --assignee "$SEARCH_MI_OID" \
    --role "Cognitive Services OpenAI User" --scope "$OPENAI_ID"
  echo "    Assigned Storage + OpenAI roles to Search service identity."
fi

# ── Patch service-account.yaml with MI client ID ─────────────────────────
echo "==> Updating k8s/service-account.yaml with MI_CLIENT_ID=$MI_CLIENT_ID"
sed -i "s/<MI_CLIENT_ID>/$MI_CLIENT_ID/g" k8s/service-account.yaml
sed -i "s/<MI_CLIENT_ID>/$MI_CLIENT_ID/g" k8s/secret-provider-class.yaml
sed -i "s/<TENANT_ID>/$TENANT_ID/g"       k8s/secret-provider-class.yaml

echo ""
echo "==> Done. Store App Registration App ID in Key Vault:"
echo "    az keyvault secret set --vault-name $KV_NAME --name AAD-APP-ID --value $APP_ID"