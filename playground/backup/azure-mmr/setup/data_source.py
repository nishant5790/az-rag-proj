"""
setup/data_source.py – Provision the Blob Storage data source for the indexer.

Uses SAS token authentication so no storage account key is stored in the
connection string. The SAS URL grants the indexer read + list access to the
blob container for the duration of the token's validity.
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


def create_data_source() -> None:
    """
    Create or update the Blob Storage data source connection.

    Builds a SAS-based connection string from the storage account name and
    SAS token, then upserts the data source via the Azure SDK.
    """
    token = cfg.BLOB_SAS_TOKEN.lstrip("?")
    conn_str = (
        f"BlobEndpoint=https://{cfg.STORAGE_ACCOUNT}.blob.core.windows.net;"
        f"SharedAccessSignature={token}"
    )

    credential = AzureKeyCredential(cfg.ADMIN_KEY)
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
