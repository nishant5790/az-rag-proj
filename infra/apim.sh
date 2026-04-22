#!/usr/bin/env bash
# infra/apim.sh - Deploy Azure API Management, import the FastAPI spec,
#                 configure Entra ID OAuth2, and apply policies.
#
# Required env vars:
#   RG           - resource group
#   LOCATION     - Azure region
#   APIM_NAME    - APIM instance name (globally unique)
#   TENANT_ID    - Azure AD tenant ID
#   APP_ID       - App Registration App ID (from aad.sh)
#   API_BACKEND  - Backend URL of the FastAPI service, e.g. https://api.mmr.company.com
#   APIM_EMAIL   - Publisher email for APIM

set -euo pipefail

echo "==> Creating APIM instance $APIM_NAME (this takes ~10-15 minutes)"
az apim create \
  --resource-group "$RG" \
  --name "$APIM_NAME" \
  --location "$LOCATION" \
  --publisher-name "MMR Platform" \
  --publisher-email "$APIM_EMAIL" \
  --sku-name Developer \
  --no-wait

echo "    Waiting for APIM to be online..."
az apim wait --resource-group "$RG" --name "$APIM_NAME" --created

# ── Import FastAPI OpenAPI spec ───────────────────────────────────────────
echo "==> Importing FastAPI OpenAPI spec"
az apim api import \
  --resource-group "$RG" \
  --service-name "$APIM_NAME" \
  --api-id mmr-api \
  --specification-url "$API_BACKEND/openapi.json" \
  --specification-format OpenApiJson \
  --display-name "MMR Search API" \
  --path api \
  --protocols https \
  --service-url "$API_BACKEND"

# ── OAuth2 authorization server (Entra ID) ───────────────────────────────
echo "==> Configuring OAuth2 server pointing to Entra ID"
az apim authz-server create \
  --resource-group "$RG" \
  --service-name "$APIM_NAME" \
  --display-name "Entra ID" \
  --client-id "$APP_ID" \
  --authorization-endpoint "https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/authorize" \
  --token-endpoint "https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token" \
  --grant-types authorizationCode \
  --bearer-token-sending-methods authorizationHeader 2>/dev/null || true

# ── Apply inbound policy (validate-jwt + rate-limit + cors) ──────────────
echo "==> Applying APIM policy"
POLICY_XML="<policies>
  <inbound>
    <base />
    <validate-jwt header-name=\"Authorization\"
                  failed-validation-httpcode=\"401\"
                  failed-validation-error-message=\"Unauthorized. Provide a valid Bearer token.\">
      <openid-config url=\"https://login.microsoftonline.com/${TENANT_ID}/v2.0/.well-known/openid-configuration\" />
      <audiences>
        <audience>api://${APP_ID}</audience>
      </audiences>
    </validate-jwt>
    <rate-limit-by-key calls=\"100\"
                       renewal-period=\"60\"
                       counter-key=\"@(context.Request.Headers.GetValueOrDefault(&quot;Authorization&quot;, &quot;anon&quot;))\" />
    <cors allow-credentials=\"false\">
      <allowed-origins>
        <origin>https://mmr.company.com</origin>
      </allowed-origins>
      <allowed-methods>
        <method>GET</method>
        <method>POST</method>
        <method>OPTIONS</method>
      </allowed-methods>
      <allowed-headers>
        <header>*</header>
      </allowed-headers>
    </cors>
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>"

az apim api policy create \
  --resource-group "$RG" \
  --service-name "$APIM_NAME" \
  --api-id mmr-api \
  --value "$POLICY_XML" \
  --format rawxml

APIM_GW=$(az apim show -g "$RG" -n "$APIM_NAME" --query gatewayUrl -o tsv)
echo ""
echo "==> APIM deployed. Gateway URL: $APIM_GW"
echo "    Call the API: POST $APIM_GW/api/search"
echo "    With header:  Authorization: Bearer <Entra ID token for api://$APP_ID>"