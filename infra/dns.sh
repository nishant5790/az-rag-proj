#!/usr/bin/env bash
# infra/dns.sh - Create Azure DNS zone and A records for the application.
#
# Run AFTER provision.sh and after the NGINX Ingress external IP is available.
#
# Required env vars:
#   RG           - resource group (same as provision.sh)
#   DNS_ZONE     - your domain, e.g. company.com
#   SUBDOMAIN    - app subdomain prefix, e.g. mmr  (creates mmr.company.com)
#   DNS_RG       - resource group where the DNS zone will live (can be same as RG)

set -euo pipefail

echo "==> Getting Ingress external IP (may take a minute after provision.sh)"
INGRESS_IP=""
while [ -z "$INGRESS_IP" ]; do
  INGRESS_IP=$(kubectl get svc ingress-nginx-controller \
    -n ingress-nginx \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  if [ -z "$INGRESS_IP" ]; then
    echo "    Waiting for Ingress IP..."
    sleep 10
  fi
done
echo "    Ingress IP: $INGRESS_IP"

# ── DNS Zone ──────────────────────────────────────────────────────────────
echo "==> Creating DNS zone $DNS_ZONE in resource group $DNS_RG"
az network dns zone create \
  --resource-group "$DNS_RG" \
  --name "$DNS_ZONE"

echo "    Name servers (delegate from your registrar):"
az network dns zone show \
  --resource-group "$DNS_RG" \
  --name "$DNS_ZONE" \
  --query nameServers -o tsv

# ── A Records ─────────────────────────────────────────────────────────────
echo "==> Creating A record: $SUBDOMAIN.$DNS_ZONE -> $INGRESS_IP"
az network dns record-set a add-record \
  --resource-group "$DNS_RG" \
  --zone-name "$DNS_ZONE" \
  --record-set-name "$SUBDOMAIN" \
  --ipv4-address "$INGRESS_IP" \
  --ttl 300

echo "==> Creating A record: api.$SUBDOMAIN.$DNS_ZONE -> $INGRESS_IP"
az network dns record-set a add-record \
  --resource-group "$DNS_RG" \
  --zone-name "$DNS_ZONE" \
  --record-set-name "api.$SUBDOMAIN" \
  --ipv4-address "$INGRESS_IP" \
  --ttl 300

# ── cert-manager ClusterIssuer ────────────────────────────────────────────
echo "==> Applying cert-manager ClusterIssuer (Let's Encrypt production)"
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: admin@${DNS_ZONE}
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            class: nginx
EOF

echo ""
echo "==> Done. DNS propagation may take up to 24h."
echo "    cert-manager will auto-issue TLS certificates once DNS resolves."