"""
config.py – Central configuration for the Azure Multimodal RAG pipeline.

Loads all environment variables from the .env file and defines derived
resource names used consistently across all modules.
"""

import os
import logging

from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ── Truststore (inject corporate CA certs into Python SSL) ────────────────────
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
except Exception as _ts_err:
    logging.getLogger(__name__).warning("Truststore injection failed: %s", _ts_err)

# ── Azure AI Search ───────────────────────────────────────────────────────────
ENDPOINT: str = os.environ["AZURE_SEARCH_SERVICE_ENDPOINT"]
ADMIN_KEY: str = os.environ["AZURE_SEARCH_ADMIN_KEY"]
INDEX_NAME: str = "pdg-was-multimodal-rag-2"
API_VERSION: str = "2024-05-01-preview"

# ── Azure Blob Storage ────────────────────────────────────────────────────────
BLOB_SAS_URL: str = os.environ["BLOB_SAS_URL"]
BLOB_SAS_TOKEN: str = os.environ["AZURE_BLOB_SAS_TOKEN"]
STORAGE_ACCOUNT: str = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
CONTAINER: str = os.environ["AZURE_BLOB_CONTAINER_NAME"]

# ── Azure OpenAI ──────────────────────────────────────────────────────────────
OPENAI_ENDPOINT: str = os.environ["AZURE_OPENAI_ENDPOINT"]
OPENAI_KEY: str = os.environ["AZURE_OPENAI_KEY"]
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_ENGINE", "text-embedding-ada-002")
EMBEDDING_DEPLOYMENT: str = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", EMBEDDING_MODEL)
EMBEDDING_DIMS: int = 1536  # ada-002 produces 1536-dimensional vectors

# ── Azure Document Intelligence ───────────────────────────────────────────────
DOC_INTEL_ENDPOINT: str = os.environ["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"]
DOC_INTEL_KEY: str = os.environ["AZURE_DOCUMENT_INTELLIGENCE_KEY"]

# ── Derived resource names ────────────────────────────────────────────────────
DS_NAME: str = f"{INDEX_NAME}-ds"
SKILLSET_NAME: str = f"{INDEX_NAME}-skillset"
INDEXER_NAME: str = f"{INDEX_NAME}-indexer"
