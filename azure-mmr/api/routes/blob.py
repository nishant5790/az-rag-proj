"""
api/routes/blob.py - GET /blob-proxy/{blob_name} endpoint.

Streams a blob from Azure Blob Storage to the client. Used by the Streamlit
frontend to fetch PDF bytes without exposing SAS tokens to the browser.

Authentication:
  - Local dev: uses AZURE_BLOB_SAS_TOKEN if set.
  - Production: uses DefaultAzureCredential (Workload Identity).
    The pod identity needs Storage Blob Data Reader on the container.
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError

import config as cfg

router = APIRouter(tags=["blob"])
logger = logging.getLogger(__name__)


def _get_blob_service_client() -> BlobServiceClient:
    if cfg.BLOB_SAS_TOKEN:
        token = cfg.BLOB_SAS_TOKEN.lstrip("?")
        account_url = f"https://{cfg.STORAGE_ACCOUNT}.blob.core.windows.net?{token}"
        return BlobServiceClient(account_url=account_url)
    account_url = f"https://{cfg.STORAGE_ACCOUNT}.blob.core.windows.net"
    return BlobServiceClient(account_url=account_url, credential=cfg.CREDENTIAL)


@router.get("/blob-proxy/{blob_name:path}", tags=["blob"])
def blob_proxy(blob_name: str) -> StreamingResponse:
    """
    Stream a blob from Azure Blob Storage.

    The Streamlit frontend calls this endpoint to fetch PDF bytes for
    rendering previews, instead of using SAS URLs directly.
    """
    try:
        client = _get_blob_service_client()
        blob_client = client.get_blob_client(container=cfg.CONTAINER, blob=blob_name)
        downloader = blob_client.download_blob()

        def _iter_chunks():
            for chunk in downloader.chunks():
                yield chunk

        return StreamingResponse(
            _iter_chunks(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'inline; filename="{blob_name.split("/")[-1]}"'},
        )
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Blob not found: {blob_name}")
    except Exception as exc:
        logger.exception("Blob proxy error for '%s'", blob_name)
        raise HTTPException(status_code=502, detail=f"Could not fetch blob: {exc}") from exc