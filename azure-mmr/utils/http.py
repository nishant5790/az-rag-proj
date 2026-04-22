"""
utils/http.py - Shared HTTP helpers for Azure AI Search REST calls.

Provides thin wrappers around requests (PUT, GET, POST, DELETE) that
automatically inject the service endpoint, API version, and auth headers.

Authentication:
  - If ADMIN_KEY is set (local dev): uses api-key header.
  - Otherwise (production): obtains a Bearer token from DefaultAzureCredential
    using the Azure AI Search scope (https://search.azure.com/.default).
"""

import logging
import requests
import config as cfg

logger = logging.getLogger(__name__)

_SEARCH_SCOPE = "https://search.azure.com/.default"


def _auth_headers() -> dict[str, str]:
    """Return auth headers: api-key for local dev, Bearer token in production."""
    if cfg.ADMIN_KEY:
        return {"api-key": cfg.ADMIN_KEY}
    token = cfg.CREDENTIAL.get_token(_SEARCH_SCOPE)
    return {"Authorization": f"Bearer {token.token}"}


def headers() -> dict[str, str]:
    """Return standard request headers including auth."""
    return {"Content-Type": "application/json", **_auth_headers()}


def _build_url(path: str) -> str:
    return f"{cfg.ENDPOINT.rstrip('/')}/{path}?api-version={cfg.API_VERSION}"


def rest_put(path: str, body: dict) -> requests.Response:
    """PUT a JSON body to an Azure AI Search REST endpoint. Raises on non-2xx."""
    resp = requests.put(_build_url(path), headers=headers(), json=body, timeout=60)
    resp.raise_for_status()
    return resp


def rest_get(path: str) -> requests.Response:
    """GET an Azure AI Search REST resource. Raises on non-2xx."""
    resp = requests.get(_build_url(path), headers=headers(), timeout=15)
    resp.raise_for_status()
    return resp


def rest_post(path: str) -> requests.Response:
    """POST (no body) to an Azure AI Search REST endpoint. Returns response as-is."""
    return requests.post(_build_url(path), headers=headers(), timeout=30)


def rest_delete(path: str) -> requests.Response:
    """DELETE an Azure AI Search REST resource. Returns response as-is."""
    return requests.delete(_build_url(path), headers=headers(), timeout=30)