"""
config.py - Central configuration for the Azure Multimodal RAG pipeline.

Loads all environment variables from the .env file and defines derived
resource names used consistently across all modules.

Authentication strategy
-----------------------
* In production (AKS + Workload Identity) no API keys are needed.
  DefaultAzureCredential picks up the pod federated token automatically.
* For local development set the API key env vars; helpers fall back to
  AzureKeyCredential / api-key headers when the keys are present.
"""

import os
import logging

from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

# Logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Truststore (inject corporate CA certs into Python SSL)
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
except Exception as _ts_err:
    logging.getLogger(__name__).warning("Truststore injection failed: %s", _ts_err)

# Azure AI Search
ENDPOINT: str = os.environ["AZURE_SEARCH_SERVICE_ENDPOINT"]
# Optional in production - not needed when Workload Identity is configured.
ADMIN_KEY: str | None = os.environ.get("AZURE_SEARCH_ADMIN_KEY")
INDEX_NAME: str = os.environ.get("AZURE_SEARCH_INDEX_NAME", "pdg-was-multimodal-rag-2")
API_VERSION: str = "2024-05-01-preview"

# Azure Blob Storage
STORAGE_ACCOUNT: str = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
CONTAINER: str = os.environ["AZURE_BLOB_CONTAINER_NAME"]
# SAS credentials - only needed for local dev; in prod use Workload Identity.
BLOB_SAS_TOKEN: str | None = os.environ.get("AZURE_BLOB_SAS_TOKEN")
BLOB_SAS_URL: str | None = os.environ.get("BLOB_SAS_URL")

# Azure OpenAI
OPENAI_ENDPOINT: str = os.environ["AZURE_OPENAI_ENDPOINT"]
# Optional in production - Search service uses its managed identity instead.
OPENAI_KEY: str | None = os.environ.get("AZURE_OPENAI_KEY")
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_ENGINE", "text-embedding-ada-002")
EMBEDDING_DEPLOYMENT: str = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", EMBEDDING_MODEL)
EMBEDDING_DIMS: int = 1536  # ada-002 produces 1536-dimensional vectors

# Azure Document Intelligence
DOC_INTEL_ENDPOINT: str = os.environ["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"]
# Optional in production - managed identity is preferred.
DOC_INTEL_KEY: str | None = os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY")

# Azure subscription context (needed for managed identity blob connection)
AZURE_SUBSCRIPTION_ID: str | None = os.environ.get("AZURE_SUBSCRIPTION_ID")
AZURE_RESOURCE_GROUP: str | None = os.environ.get("AZURE_RESOURCE_GROUP")

# Managed Identity credential (used everywhere keys are absent)
# DefaultAzureCredential chain:
#   1. Env vars  (AZURE_CLIENT_ID / TENANT_ID / CLIENT_SECRET)
#   2. Workload Identity (AKS pod with federated token)
#   3. Azure CLI (local dev)
CREDENTIAL: DefaultAzureCredential = DefaultAzureCredential()

# Derived resource names
DS_NAME: str = f"{INDEX_NAME}-ds"
SKILLSET_NAME: str = f"{INDEX_NAME}-skillset"
INDEXER_NAME: str = f"{INDEX_NAME}-indexer"