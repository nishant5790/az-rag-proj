"""
api/routes/pipeline.py - Pipeline management endpoints (setup / teardown / status).

These are admin operations. In production they should be called via a Kubernetes
Job or protected by an admin-only Entra ID scope, not exposed to end users.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, BackgroundTasks, HTTPException

from setup.indexer import get_indexer_status
import pipeline as pl

router = APIRouter(tags=["pipeline"])
logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1)


@router.get("/status")
def indexer_status() -> dict:
    """Return the current indexer run status."""
    try:
        return get_indexer_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch indexer status: {exc}") from exc


@router.post("/setup")
def pipeline_setup(background_tasks: BackgroundTasks) -> dict:
    """
    Provision the full pipeline (data source, index, skillset, indexer) and
    trigger an indexer run. Runs asynchronously; poll /pipeline/status for progress.
    """
    background_tasks.add_task(_run_setup)
    return {"message": "Pipeline setup started. Poll /pipeline/status for progress."}


@router.post("/teardown")
def pipeline_teardown(background_tasks: BackgroundTasks) -> dict:
    """Delete all pipeline resources. Runs asynchronously."""
    background_tasks.add_task(_run_teardown)
    return {"message": "Pipeline teardown started."}


def _run_setup() -> None:
    try:
        pl.setup()
    except Exception:
        logger.exception("Pipeline setup failed.")


def _run_teardown() -> None:
    try:
        pl.teardown()
    except Exception:
        logger.exception("Pipeline teardown failed.")