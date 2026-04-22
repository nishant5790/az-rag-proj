"""
api/main.py - FastAPI application for the Azure Multimodal RAG pipeline.

Exposes:
  POST   /search                - run BM25 search, return ranked results
  GET    /blob-proxy/{name}     - stream a blob from Azure Blob Storage
  GET    /pipeline/status       - current indexer status
  POST   /pipeline/setup        - provision the full pipeline (admin)
  POST   /pipeline/teardown     - delete all pipeline resources (admin)
  GET    /healthz               - liveness probe
"""

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.search import router as search_router
from api.routes.pipeline import router as pipeline_router
from api.routes.blob import router as blob_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = FastAPI(
    title="Azure MMR API",
    description="Multimodal RAG search and pipeline management API",
    version="1.0.0",
)
_truststore_active = False
try:
    import truststore

    truststore.inject_into_ssl()
    _truststore_active = True
    print("Truststore SSL injection successful.")
except ImportError:
    print(
        "Truststore not installed. If you see CERTIFICATE_VERIFY_FAILED errors, "
        "run the app using the project venv (.venv) or install truststore."
    )
except Exception as e:
    print(f"Truststore injection failed: {e}")
# CORS: allow Streamlit origin and APIM gateway origin.
# Restrict to specific origins in production via ALLOWED_ORIGINS env var.
_allowed = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(search_router)
app.include_router(blob_router)
app.include_router(pipeline_router, prefix="/pipeline")


@app.get("/healthz", tags=["ops"])
def healthz():
    return {"status": "ok"}