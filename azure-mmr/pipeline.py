"""
pipeline.py – Orchestrate full setup and teardown of the multimodal RAG pipeline.

setup()    – provisions all Azure resources in dependency order, triggers
             the indexer, and waits for completion.
teardown() – deletes all resources in reverse dependency order so no
             orphaned resources remain.
"""

import logging

import config as cfg
from setup import (
    create_data_source,
    create_index,
    create_skillset,
    create_indexer,
    run_indexer,
    wait_for_indexer,
)
from utils.http import rest_delete

logger = logging.getLogger(__name__)


def setup() -> None:
    """
    Provision the full MMR pipeline in dependency order:

    1. Data source  – Blob Storage SAS connection
    2. Index        – schema, HNSW vector config, semantic config
    3. Skillset     – Doc Intelligence + OCR + Merge + Split + Embeddings
    4. Indexer      – wires data source, skillset, and index together

    After provisioning, triggers an indexer run and blocks until it finishes.
    """
    logger.info("─── Step 1/4: Data Source ───────────────────────────────────")
    create_data_source()

    logger.info("─── Step 2/4: Index ─────────────────────────────────────────")
    create_index()

    logger.info("─── Step 3/4: Skillset ──────────────────────────────────────")
    create_skillset()

    logger.info("─── Step 4/4: Indexer ───────────────────────────────────────")
    create_indexer()
    run_indexer()

    logger.info("Pipeline provisioned. Waiting for indexer to complete...")
    result = wait_for_indexer()

    if result.get("status") == "success":
        logger.info("Setup complete — index is ready for queries.")
    else:
        logger.error("Setup finished with errors. Review logs above.")


def teardown() -> None:
    """
    Delete all pipeline resources in reverse dependency order:
      indexer → skillset → data source → index

    404 responses are treated as already-deleted (idempotent).
    """
    resources = [
        ("indexers",    cfg.INDEXER_NAME),
        ("skillsets",   cfg.SKILLSET_NAME),
        ("datasources", cfg.DS_NAME),
        ("indexes",     cfg.INDEX_NAME),
    ]
    for kind, name in resources:
        resp = rest_delete(f"{kind}/{name}")
        if resp.status_code == 204:
            logger.info("Deleted %s '%s'.", kind.rstrip("s"), name)
        elif resp.status_code == 404:
            logger.info("%s '%s' not found (already deleted).", kind.rstrip("s"), name)
        else:
            logger.error(
                "Failed to delete %s '%s': %s %s",
                kind, name, resp.status_code, resp.text,
            )
    logger.info("Teardown complete.")
