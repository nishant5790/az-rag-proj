"""
setup/indexer.py – Create, trigger, and monitor the Azure AI Search indexer.

The indexer ties together the data source, skillset, and target index.
It is responsible for:
  - Extracting raw content and metadata from blobs
  - Running the enrichment skillset on each document
  - Writing both source metadata fields and enriched output fields to the index

Field mappings
  metadata_storage_path   → id (base64-encoded, used as the unique key)
  metadata_storage_name   → blob_name
  metadata_storage_path   → blob_url
  metadata_storage_last_modified → last_modified
  metadata_title          → title

Output field mappings (from skillset enrichment tree)
  /document/layout_text   → layout_text
  /document/merged_content → merged_content
  /document/pages          → pages
  /document/content_vector → content_vector
"""

import time
import logging

import config as cfg
from utils.http import rest_put, rest_post, rest_get

logger = logging.getLogger(__name__)


def create_indexer() -> None:
    """Create or update the indexer with all field and output field mappings."""
    body = {
        "name": cfg.INDEXER_NAME,
        "description": "Multimodal RAG indexer for PDF documents in Blob Storage",
        "dataSourceName": cfg.DS_NAME,
        "skillsetName": cfg.SKILLSET_NAME,
        "targetIndexName": cfg.INDEX_NAME,
        "parameters": {
            "configuration": {
                "dataToExtract": "contentAndMetadata",
                # Rasterise embedded images so OcrSkill can process them
                "imageAction": "generateNormalizedImages",
                "parsingMode": "default",
                # Required for DocumentIntelligenceLayoutSkill to receive
                # the raw file bytes via /document/file_data
                "allowSkillsetToReadFileData": True,
            }
        },
        "fieldMappings": [
            {
                "sourceFieldName": "metadata_storage_path",
                "targetFieldName": "id",
                "mappingFunction": {"name": "base64Encode"},
            },
            {"sourceFieldName": "metadata_storage_name", "targetFieldName": "blob_name"},
            {"sourceFieldName": "metadata_storage_path", "targetFieldName": "blob_url"},
            {"sourceFieldName": "metadata_storage_last_modified", "targetFieldName": "last_modified"},
            {"sourceFieldName": "metadata_title", "targetFieldName": "title"},
        ],
        "outputFieldMappings": [
            {"sourceFieldName": "/document/layout_text", "targetFieldName": "layout_text"},
            {"sourceFieldName": "/document/merged_content", "targetFieldName": "merged_content"},
            {"sourceFieldName": "/document/pages", "targetFieldName": "pages"},
            {"sourceFieldName": "/document/content_vector", "targetFieldName": "content_vector"},
        ],
    }
    rest_put(f"indexers/{cfg.INDEXER_NAME}", body)
    logger.info("Indexer '%s' created.", cfg.INDEXER_NAME)


def run_indexer() -> None:
    """Trigger an indexer run (non-blocking)."""
    resp = rest_post(f"indexers/{cfg.INDEXER_NAME}/run")
    if resp.status_code == 202:
        logger.info("Indexer run triggered.")
    elif resp.status_code == 409:
        logger.info("Indexer is already running.")
    else:
        resp.raise_for_status()


def wait_for_indexer(poll_interval: int = 15, timeout: int = 900) -> dict:
    """
    Poll the indexer status until success or failure.

    Args:
        poll_interval: Seconds between status polls (default 15).
        timeout:       Max seconds to wait before raising TimeoutError (default 900).

    Returns:
        The ``lastResult`` dict from the indexer status response.

    Raises:
        TimeoutError: If the indexer does not complete within ``timeout`` seconds.
    """
    deadline = time.time() + timeout
    prev_status = None

    while time.time() < deadline:
        data = rest_get(f"indexers/{cfg.INDEXER_NAME}/status").json()
        last_run = data.get("lastResult") or {}
        status = last_run.get("status", "unknown")
        items_ok = last_run.get("itemsProcessed", 0)
        items_fail = last_run.get("itemsFailed", 0)

        if status != prev_status:
            logger.info(
                "Indexer status: %s  (processed=%s, failed=%s)",
                status, items_ok, items_fail,
            )
            prev_status = status

        if status == "success":
            logger.info("Indexer completed successfully. %d documents indexed.", items_ok)
            return last_run

        if status in ("transientFailure", "persistentFailure"):
            logger.error("Indexer finished with status: %s", status)
            for err in last_run.get("errors", []):
                logger.error(
                    "  [%s] %s — %s",
                    err.get("name", "?"),
                    err.get("errorMessage", ""),
                    err.get("details", ""),
                )
            for warn in last_run.get("warnings", []):
                logger.warning(
                    "  [%s] %s — %s",
                    warn.get("name", "?"),
                    warn.get("message", ""),
                    warn.get("details", ""),
                )
            return last_run

        if status == "inProgress":
            logger.info("  ... in progress: %d processed, %d failed so far", items_ok, items_fail)

        time.sleep(poll_interval)

    raise TimeoutError(
        f"Indexer did not complete within {timeout}s. Check the Azure portal for details."
    )
