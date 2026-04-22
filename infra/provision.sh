#!/usr/bin/env bash
# infra/provision.sh - Provision ACR, AKS, Key Vault, and Helm addons.
# Run from Azure Cloud Shell or any machine with az + helm + kubectl installed.
#
# Usage:
#   export RG=rg-mmr-prod
#   export LOCATION=eastus
#   export CLUSTER_NAME=aks-mmr-prod
#   export ACR_NAME=acrmmrprod           # must be globally unique, 5-50 chars
#   export KV_NAME=kv-mmr-prod           # must be globally unique
#   export SUBSCRIPTION=$(az account show --query id -o tsv)
#   bash infra/provision.sh

set -euo pipefail

echo "==> Creating resource group $RG in $LOCATION"
az group create --name "$RG" --location "$LOCATION"

# ── Azure Container Registry ───────────────────────────────────────────────
echo "==> Creating ACR $ACR_NAME"
az acr create \
  --resource-group "$RG" \
  --name "$ACR_NAME" \
  --sku Standard \
  --admin-enabled false

# ── Azure Key Vault ────────────────────────────────────────────────────────
echo "==> Creating Key Vault $KV_NAME"
az keyvault create \
  --resource-group "$RG" \
  --name "$KV_NAME" \
  --location "$LOCATION" \
  --enable-rbac-authorization true   # use Azure RBAC, not vault access policies

# ── AKS Cluster ───────────────────────────────────────────────────────────
echo "==> Creating AKS cluster $CLUSTER_NAME (Workload Identity + OIDC enabled)"
az aks create \
  --resource-group "$RG" \
  --name "$CLUSTER_NAME" \
  --node-count 2 \
  --node-vm-size Standard_D2s_v5 \
  --enable-oidc-issuer \
  --enable-workload-identity \
  --attach-acr "$ACR_NAME" \
  --generate-ssh-keys \
  --network-plugin azure \
  --enable-addons monitoring

# Retrieve credentials
az aks get-credentials --resource-group "$RG" --name "$CLUSTER_NAME" --overwrite-existing

# ── Helm: NGINX Ingress ────────────────────────────────────────────────────
echo "==> Installing NGINX Ingress Controller"
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/azure-load-balancer-health-probe-request-path"=/healthz

# ── Helm: cert-manager ────────────────────────────────────────────────────
echo "==> Installing cert-manager"
helm repo add jetstack https://charts.jetstack.io
helm repo update
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --set installCRDs=true

# ── Helm: Key Vault CSI Driver ────────────────────────────────────────────
echo "==> Installing Azure Key Vault CSI Driver"
helm repo add csi-secrets-store-provider-azure \
  https://azure.github.io/secrets-store-csi-driver-provider-azure/charts
helm repo update
helm upgrade --install csi-secrets-store-provider-azure \
  csi-secrets-store-provider-azure/csi-secrets-store-provider-azure \
  --namespace kube-system \
  --set syncSecret.enabled=true

echo ""
echo "==> Done. Next steps:"
echo "    1. Get ingress IP:  kubectl get svc -n ingress-nginx ingress-nginx-controller"
echo "    2. Run infra/aad.sh to set up Workload Identity and RBAC"
echo "    3. Run infra/dns.sh  to create DNS records (requires the ingress IP)"
echo "    4. Run infra/apim.sh to deploy APIM"