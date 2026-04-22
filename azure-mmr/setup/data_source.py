"""
setup/data_source.py - Provision the Blob Storage data source for the indexer.

Authentication strategy:
  - Production (managed identity): uses the Search service system-assigned MI
    with a ResourceId connection string. Requires Storage Blob Data Reader on
    the storage account assigned to the Search service identity.
  - Local dev (SAS token): builds a SAS-based connection string when
    AZURE_BLOB_SAS_TOKEN is set.
"""

import logging

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes import SearchIndexerClient
from azure.search.documents.indexes.models import (
    SearchIndexerDataSourceConnection,
    SearchIndexerDataContainer,
)

import config as cfg

logger = logging.getLogger(__name__)


def _make_connection_string() -> str:
    """
    Build the blob connection string for the indexer.

    Uses SAS token when available (local dev); otherwise uses a managed identity
    ResourceId connection string (production).
    """
    if cfg.BLOB_SAS_TOKEN:
        token = cfg.BLOB_SAS_TOKEN.lstrip("?")
        return (
            f"BlobEndpoint=https://{cfg.STORAGE_ACCOUNT}.blob.core.windows.net;"
            f"SharedAccessSignature={token}"
        )
    if cfg.AZURE_SUBSCRIPTION_ID and cfg.AZURE_RESOURCE_GROUP:
        return (
            f"ResourceId=/subscriptions/{cfg.AZURE_SUBSCRIPTION_ID}"
            f"/resourceGroups/{cfg.AZURE_RESOURCE_GROUP}"
            f"/providers/Microsoft.Storage/storageAccounts/{cfg.STORAGE_ACCOUNT};"
        )
    raise EnvironmentError(
        "Neither AZURE_BLOB_SAS_TOKEN nor "
        "(AZURE_SUBSCRIPTION_ID + AZURE_RESOURCE_GROUP) are set. "
        "Cannot build blob connection string."
    )


def create_data_source() -> None:
    """Create or update the Blob Storage data source connection."""
    conn_str = _make_connection_string()
    credential = AzureKeyCredential(cfg.ADMIN_KEY) if cfg.ADMIN_KEY else cfg.CREDENTIAL
    indexer_client = SearchIndexerClient(cfg.ENDPOINT, credential)

    ds = SearchIndexerDataSourceConnection(
        name=cfg.DS_NAME,
        type="azureblob",
        connection_string=conn_str,
        container=SearchIndexerDataContainer(name=cfg.CONTAINER),
        description=f"Blob source for multimodal RAG ({cfg.CONTAINER})",
    )
    indexer_client.create_or_update_data_source_connection(ds)
    logger.info("Data source '%s' ready.", cfg.DS_NAME)