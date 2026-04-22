"""
mmr-search.py – Multimodal RAG: Azure AI Search + Doc Intelligence + OpenAI Embeddings.

Creates:
  1. Data Source  → Blob container (SAS auth)
  2. Index        → Schema with vector field (text-embedding-ada-002, 1536-d)
  3. Skillset     → Doc Intelligence layout + OCR + Merge + Split + OpenAI Embeddings
  4. Indexer      → Wires everything together

Usage:
    python mmr-search.py setup     # provision index + indexer
    python mmr-search.py query <text>  # search the index
"""

import os
import sys
import time
import logging

import requests
from dotenv import load_dotenv
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient, SearchIndexerClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SearchFieldDataType,
    SimpleField,
    SearchableField,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticSearch,
    SemanticPrioritizedFields,
    SemanticField,
    SearchIndexerDataSourceConnection,
    SearchIndexerDataContainer,
    SearchIndexer,
    IndexingParameters,
    SearchIndexerSkillset,
    InputFieldMappingEntry,
    OutputFieldMappingEntry,
    OcrSkill,
    MergeSkill,
    SplitSkill,
    TextSplitMode,
    DocumentIntelligenceLayoutSkill,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Environment variables ─────────────────────────────────────────────────────
ENDPOINT = os.environ["AZURE_SEARCH_SERVICE_ENDPOINT"]
ADMIN_KEY = os.environ["AZURE_SEARCH_ADMIN_KEY"]
INDEX_NAME = "pdg-was-multimodal-rag-2"

BLOB_SAS_URL = os.environ["BLOB_SAS_URL"]
BLOB_SAS_TOKEN = os.environ["AZURE_BLOB_SAS_TOKEN"]
STORAGE_ACCOUNT = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
CONTAINER = os.environ["AZURE_BLOB_CONTAINER_NAME"]

OPENAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
OPENAI_KEY = os.environ["AZURE_OPENAI_KEY"]
EMBEDDING_MODEL = os.getenv("EMBEDDING_ENGINE", "text-embedding-ada-002")
EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", EMBEDDING_MODEL)
EMBEDDING_DIMS = 1536  # ada-002 produces 1536-d vectors

DOC_INTEL_ENDPOINT = os.environ["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"]
DOC_INTEL_KEY = os.environ["AZURE_DOCUMENT_INTELLIGENCE_KEY"]

DS_NAME = f"{INDEX_NAME}-ds"
SKILLSET_NAME = f"{INDEX_NAME}-skillset"
INDEXER_NAME = f"{INDEX_NAME}-indexer"

API_VERSION = "2024-05-01-preview"

# ── Truststore ────────────────────────────────────────────────────────────────
try:
    import truststore
    truststore.inject_into_ssl()
    print("Truststore SSL injection successful.")
except ImportError:
    print("Truststore not installed – using default SSL.")
except Exception as e:
    print(f"Truststore injection failed: {e}")


# ── REST helpers ──────────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "api-key": ADMIN_KEY}


def _rest_put(path: str, body: dict) -> requests.Response:
    url = f"{ENDPOINT.rstrip('/')}/{path}?api-version={API_VERSION}"
    resp = requests.put(url, headers=_headers(), json=body, timeout=60)
    resp.raise_for_status()
    return resp


# ── 1. Data Source ────────────────────────────────────────────────────────────

def create_data_source() -> None:
    token = BLOB_SAS_TOKEN.lstrip("?")
    conn_str = (
        f"BlobEndpoint=https://{STORAGE_ACCOUNT}.blob.core.windows.net;"
        f"SharedAccessSignature={token}"
    )
    credential = AzureKeyCredential(ADMIN_KEY)
    indexer_client = SearchIndexerClient(ENDPOINT, credential)

    ds = SearchIndexerDataSourceConnection(
        name=DS_NAME,
        type="azureblob",
        connection_string=conn_str,
        container=SearchIndexerDataContainer(name=CONTAINER),
        description=f"Blob source for multimodal RAG ({CONTAINER})",
    )
    indexer_client.create_or_update_data_source_connection(ds)
    logger.info("Data source '%s' ready.", DS_NAME)


# ── 2. Index ──────────────────────────────────────────────────────────────────

def create_index() -> None:
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="blob_name", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="blob_url", type=SearchFieldDataType.String, filterable=False),
        SimpleField(name="last_modified", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="merged_content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="layout_text", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="image_text", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SimpleField(name="pages", type=SearchFieldDataType.Collection(SearchFieldDataType.String), searchable=True),
        SearchableField(name="title", type=SearchFieldDataType.String),
        # Vector field for OpenAI embeddings
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBEDDING_DIMS,
            vector_search_profile_name="hnsw-profile",
        ),
    ]

    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="hnsw-algo")],
        profiles=[VectorSearchProfile(name="hnsw-profile", algorithm_configuration_name="hnsw-algo")],
    )

    semantic_search = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name="mmr-semantic",
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[
                        SemanticField(field_name="merged_content"),
                        SemanticField(field_name="layout_text"),
                    ],
                ),
            )
        ]
    )

    credential = AzureKeyCredential(ADMIN_KEY)
    index_client = SearchIndexClient(ENDPOINT, credential)
    index = SearchIndex(
        name=INDEX_NAME,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
    )
    index_client.create_or_update_index(index)
    logger.info("Index '%s' ready.", INDEX_NAME)


# ── 3. Skillset ───────────────────────────────────────────────────────────────

def create_skillset() -> None:
    """
    Pipeline (built entirely via REST):
      Doc Intelligence Layout → OCR → Merge → Split → OpenAI Embeddings
        Build the enrichment pipeline:

          ┌─────────────────────────────────────────────────────────────────┐
          │ /document/content (raw blob bytes)                              │
          │         │                                                        │
          │  ┌──────▼──────────────────────┐                               │
          │  │ DocumentIntelligenceLayout   │  ← tables, headings, layout  │
          │  └──────┬──────────────────────┘                               │
          │         │ /document/layout_text                                 │
          │  ┌──────▼──────┐   ┌─────────────────────┐                    │
          │  │  OcrSkill   │   │  KeyPhrase + Entity  │                    │
          │  └──────┬──────┘   └──────────────────────┘                   │
          │         │ /document/image_text                                  │
          │  ┌──────▼──────────────────────┐                               │
          │  │       MergeSkill            │  ← content + image_text       │
          │  └──────┬──────────────────────┘                               │
          │         │ /document/merged_content                              │
          │  ┌──────▼──────────────────────┐                               │
          │  │       SplitSkill            │  ← chunk into pages           │
          │  └──────┬──────────────────────┘                               │
          │         │ /document/pages/*                                     │
          │  ┌──────▼──────────────────────┐  (only if ENABLE_VECTOR)      │
          │  │  AzureOpenAIEmbeddingSkill  │                               │
          │  └─────────────────────────────┘                               │
          └─────────────────────────────────────────────────────────────────┘

    """
    body = {
        "name": SKILLSET_NAME,
        "description": "Multimodal RAG: Doc Intelligence + OCR + Merge + Split + Embeddings",
        "skills": [
            # 1. Document Intelligence Layout
            {
                "@odata.type": "#Microsoft.Skills.Util.DocumentIntelligenceLayoutSkill",
                "name": "doc-intelligence-layout",
                "description": "Extract text, tables, and layout from PDFs",
                "context": "/document",
                "inputs": [{"name": "file_data", "source": "/document/file_data"}],
                "outputs": [{"name": "markdown_document", "targetName": "layout_text"}],
            },
            # 2. OCR for embedded images
            {
                "@odata.type": "#Microsoft.Skills.Vision.OcrSkill",
                "name": "ocr-images",
                "description": "OCR on images embedded in PDFs",
                "context": "/document/normalized_images/*",
                "defaultLanguageCode": "en",
                "detectOrientation": True,
                "inputs": [{"name": "image", "source": "/document/normalized_images/*"}],
                "outputs": [
                    {"name": "text", "targetName": "image_text"},
                    {"name": "layoutText", "targetName": "image_layout_text"},
                ],
            },
            # 3. Merge content + OCR text
            {
                "@odata.type": "#Microsoft.Skills.Text.MergeSkill",
                "name": "merge-content",
                "description": "Merge raw content with OCR text from images",
                "context": "/document",
                "insertPreTag": " ",
                "insertPostTag": " ",
                "inputs": [
                    {"name": "text", "source": "/document/content"},
                    {"name": "itemsToInsert", "source": "/document/normalized_images/*/image_text"},
                    {"name": "offsets", "source": "/document/normalized_images/*/contentOffset"},
                ],
                "outputs": [{"name": "mergedText", "targetName": "merged_content"}],
            },
            # 4. Split into chunks
            {
                "@odata.type": "#Microsoft.Skills.Text.SplitSkill",
                "name": "split-pages",
                "description": "Split merged content into searchable chunks",
                "context": "/document",
                "textSplitMode": "pages",
                "maximumPageLength": 2000,
                "pageOverlapLength": 200,
                "inputs": [{"name": "text", "source": "/document/merged_content"}],
                "outputs": [{"name": "textItems", "targetName": "pages"}],
            },
            # 5. OpenAI Embeddings
            {
                "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
                "name": "openai-embeddings",
                "description": "Generate vector embeddings via Azure OpenAI",
                "context": "/document",
                "resourceUri": OPENAI_ENDPOINT.rstrip("/"),
                "apiKey": OPENAI_KEY,
                "deploymentId": EMBEDDING_DEPLOYMENT,
                "modelName": EMBEDDING_MODEL,
                "inputs": [{"name": "text", "source": "/document/merged_content"}],
                "outputs": [{"name": "embedding", "targetName": "content_vector"}],
            },
        ],
    }

    url = f"{ENDPOINT.rstrip('/')}/skillsets/{SKILLSET_NAME}?api-version={API_VERSION}"
    resp = requests.put(url, headers=_headers(), json=body, timeout=60)
    if not resp.ok:
        logger.error("Skillset PUT failed (%s): %s", resp.status_code, resp.text)
    resp.raise_for_status()
    logger.info("Skillset '%s' ready.", SKILLSET_NAME)


# ── 4. Indexer ────────────────────────────────────────────────────────────────

def create_indexer() -> None:
    body = {
        "name": INDEXER_NAME,
        "description": "Multimodal RAG indexer for PDFs",
        "dataSourceName": DS_NAME,
        "skillsetName": SKILLSET_NAME,
        "targetIndexName": INDEX_NAME,
        "parameters": {
            "configuration": {
                "dataToExtract": "contentAndMetadata",
                "imageAction": "generateNormalizedImages",
                "parsingMode": "default",
            }
        },
        "fieldMappings": [
            {"sourceFieldName": "metadata_storage_path", "targetFieldName": "id",
             "mappingFunction": {"name": "base64Encode"}},
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
    _rest_put(f"indexers/{INDEXER_NAME}", body)
    logger.info("Indexer '%s' created.", INDEXER_NAME)


def run_indexer() -> None:
    url = f"{ENDPOINT.rstrip('/')}/indexers/{INDEXER_NAME}/run?api-version={API_VERSION}"
    resp = requests.post(url, headers=_headers(), timeout=30)
    if resp.status_code == 202:
        logger.info("Indexer run triggered.")
    elif resp.status_code == 409:
        logger.info("Indexer is already running.")
    else:
        resp.raise_for_status()


def wait_for_indexer(poll_interval: int = 15, timeout: int = 900) -> dict:
    """Poll indexer until it finishes. Log detailed errors on failure."""
    url = f"{ENDPOINT.rstrip('/')}/indexers/{INDEXER_NAME}/status?api-version={API_VERSION}"
    deadline = time.time() + timeout
    prev_status = None

    while time.time() < deadline:
        resp = requests.get(url, headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        last_run = data.get("lastResult") or {}
        status = last_run.get("status", "unknown")
        items_ok = last_run.get("itemsProcessed", 0)
        items_fail = last_run.get("itemsFailed", 0)

        if status != prev_status:
            logger.info("Indexer status: %s  (processed=%s, failed=%s)", status, items_ok, items_fail)
            prev_status = status

        if status == "success":
            logger.info("Indexer completed successfully. %d documents indexed.", items_ok)
            return last_run

        if status in ("transientFailure", "persistentFailure"):
            logger.error("Indexer finished with status: %s", status)
            # Log item-level errors from lastResult
            for err in last_run.get("errors", []):
                logger.error(
                    "  [%s] %s — %s",
                    err.get("name", "?"),
                    err.get("errorMessage", ""),
                    err.get("details", ""),
                )
            # Log warnings too
            for warn in last_run.get("warnings", []):
                logger.warning(
                    "  [%s] %s — %s",
                    warn.get("name", "?"),
                    warn.get("message", ""),
                    warn.get("details", ""),
                )
            return last_run

        if status == "inProgress":
            # Show per-item progress
            logger.info("  ... in progress: %d processed, %d failed so far", items_ok, items_fail)

        time.sleep(poll_interval)

    raise TimeoutError(f"Indexer did not complete within {timeout}s. Check Azure portal for details.")


# ── Teardown ──────────────────────────────────────────────────────────────────

def teardown() -> None:
    """Delete indexer, skillset, data source, and index (in dependency order)."""
    resources = [
        ("indexers",    INDEXER_NAME),
        ("skillsets",   SKILLSET_NAME),
        ("datasources", DS_NAME),
        ("indexes",     INDEX_NAME),
    ]
    for kind, name in resources:
        url = f"{ENDPOINT.rstrip('/')}/{kind}/{name}?api-version={API_VERSION}"
        resp = requests.delete(url, headers=_headers(), timeout=30)
        if resp.status_code == 204:
            logger.info("Deleted %s '%s'.", kind.rstrip('s'), name)
        elif resp.status_code == 404:
            logger.info("%s '%s' not found (already deleted).", kind.rstrip('s'), name)
        else:
            logger.error("Failed to delete %s '%s': %s %s", kind, name, resp.status_code, resp.text)
    logger.info("Teardown complete.")


# ── Setup (all-in-one) ───────────────────────────────────────────────────────

def setup() -> None:
    create_data_source()
    create_index()
    create_skillset()
    create_indexer()
    run_indexer()
    logger.info("Pipeline provisioned. Waiting for indexer to complete...")
    result = wait_for_indexer()
    if result.get("status") == "success":
        logger.info("Setup finished — index is ready for queries.")
    else:
        logger.error("Setup finished with errors. Review logs above.")


# ── Search / Query ────────────────────────────────────────────────────────────

class MMRSearch:
    """Hybrid search: full-text + vector against the multimodal index."""

    def __init__(self):
        credential = AzureKeyCredential(ADMIN_KEY)
        self.client = SearchClient(ENDPOINT, INDEX_NAME, credential)

    def search(self, query: str, top: int = 5):
        results = self.client.search(search_text=query, top=top)
        hits = []
        for r in results:
            hits.append({
                "score": r["@search.score"],
                "file": r.get("blob_name", ""),
                "snippet": (r.get("merged_content") or r.get("content") or "")[:400],
            })
        return hits

    def print_results(self, query: str, top: int = 5) -> None:
        print(f"\n{'='*60}")
        print(f"Query: {query}")
        print(f"{'='*60}")
        for i, hit in enumerate(self.search(query, top), 1):
            print(f"\n--- Result {i} (score {hit['score']:.4f}) ---")
            print(f"File : {hit['file']}")
            print(f"Snippet: {hit['snippet']}...")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "setup"

    if cmd == "setup":
        setup()
    elif cmd == "query":
        q = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "summary"
        MMRSearch().print_results(q)
    elif cmd == "delete":
        teardown()
    elif cmd == "status":
        result = wait_for_indexer(poll_interval=5, timeout=30)
        print(f"Status: {result.get('status')}  |  Processed: {result.get('itemsProcessed',0)}  |  Failed: {result.get('itemsFailed',0)}")
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python mmr-search.py [setup|query <text>|delete|status]")
