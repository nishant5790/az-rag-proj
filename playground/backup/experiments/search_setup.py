"""
search_setup.py – Provisions the full Azure AI Search pipeline.

Creates (or updates) in this order:
  1. Data Source  → points to the Azure Blob container
  2. Index        → defines the searchable schema
  3. Skillset     → enrichment chain (Doc Intelligence → OCR → Merge → Split
                    → optionally Azure OpenAI embeddings)
  4. Indexer      → wires data source + skillset + index together and runs it

All resources are idempotent: calling setup() on an existing deployment will
update them to the latest definition rather than error out.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes import SearchIndexClient, SearchIndexerClient
from azure.search.documents.indexes.models import (
    # Index / fields
    SearchIndex,
    SearchField,
    SearchFieldDataType,
    SimpleField,
    SearchableField,
    ComplexField,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticSearch,
    SemanticPrioritizedFields,
    SemanticField,
    # Indexer / data source
    SearchIndexerDataSourceConnection,
    SearchIndexerDataContainer,
    SearchIndexerDataSourceType,
    SearchIndexer,
    IndexingParameters,
    IndexingParametersConfiguration,
    BlobIndexerDataToExtract,
    BlobIndexerImageAction,
    # Skillset
    SearchIndexerSkillset,
    InputFieldMappingEntry,
    OutputFieldMappingEntry,
)
# Truststore activation
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

TRUSTSTORE_ACTIVE = _truststore_active

from config import AppConfig

logger = logging.getLogger(__name__)

# ─── REST helpers (for resources not yet in the SDK) ──────────────────────────

def _rest_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "api-key": api_key,
    }


def _put(endpoint: str, api_key: str, path: str, body: dict) -> requests.Response:
    url = f"{endpoint.rstrip('/')}/{path}?api-version=2024-11-01-preview"
    resp = requests.put(url, headers=_rest_headers(api_key), json=body, timeout=60)
    resp.raise_for_status()
    return resp


# ─── Main class ───────────────────────────────────────────────────────────────

class SearchPipelineSetup:
    """
    Provisions and manages the full Azure AI Search pipeline for PDF ingestion.
    """

    API_VERSION = "2024-11-01-preview"

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._credential = AzureKeyCredential(cfg.search.admin_key)
        self._index_client = SearchIndexClient(
            endpoint=cfg.search.endpoint,
            credential=self._credential,
        )
        self._indexer_client = SearchIndexerClient(
            endpoint=cfg.search.endpoint,
            credential=self._credential,
        )

    # ── 1. Data Source ────────────────────────────────────────────────────────

    @staticmethod
    def _build_connection_string(connection_string_or_sas_url: str) -> str:
        """
        Normalize the input into the format Azure AI Search expects.

        Accepted inputs
        ───────────────
        • Full Azure Storage connection string (starts with "DefaultEndpointsProtocol=")
          → returned as-is.
        • SAS URL (starts with "https://")
          → parsed into "BlobEndpoint=…;SharedAccessSignature=…" format.
        """
        value = connection_string_or_sas_url.strip()
        if value.startswith("DefaultEndpointsProtocol=") or value.startswith("BlobEndpoint="):
            return value  # already a proper connection string

        if value.startswith("https://"):
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(value)
            # Blob endpoint = scheme + netloc only (no path, no query)
            blob_endpoint = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
            sas_token = parsed.query.lstrip("?")
            if not sas_token:
                raise ValueError(
                    "SAS URL has no query string – expected a URL of the form "
                    "https://<account>.blob.core.windows.net/…?<sas-token>"
                )
            return f"BlobEndpoint={blob_endpoint};SharedAccessSignature={sas_token}"

        raise ValueError(
            "Unrecognised credential format. Pass either a full Azure Storage "
            "connection string or a SAS URL (https://…?<sas-token>)."
        )

    def create_data_source(self, connection_string_or_sas_url: str) -> None:
        """
        Create (or update) a Blob Storage data source connection.

        Parameters
        ----------
        connection_string_or_sas_url :
            Either a full Azure Storage connection string
            (``DefaultEndpointsProtocol=https;AccountName=…``) **or** a SAS URL
            (``https://<account>.blob.core.windows.net/…?<sas-token>``).
            Both formats are accepted and normalised automatically.
        """
        conn_str = self._build_connection_string(connection_string_or_sas_url)
        data_source = SearchIndexerDataSourceConnection(
            name=self.cfg.search.datasource_name,
            type=SearchIndexerDataSourceType.AZURE_BLOB,
            connection_string=conn_str,
            container=SearchIndexerDataContainer(
                name=self.cfg.blob.container_name,
                query=None,  # index all blobs; set a prefix here to filter
            ),
            description="PDF documents stored in Azure Blob Storage",
        )
        self._indexer_client.create_or_update_data_source_connection(data_source)
        logger.info("Data source '%s' ready.", self.cfg.search.datasource_name)

    # ── 2. Index ──────────────────────────────────────────────────────────────

    def _build_fields(self) -> list[SearchField]:
        """Define the index schema."""
        fields: list[SearchField] = [
            # ── Core identifiers ──────────────────────────────────────────
            SimpleField(
                name="id",
                type=SearchFieldDataType.String,
                key=True,
                filterable=True,
            ),
            SimpleField(
                name="blob_name",
                type=SearchFieldDataType.String,
                filterable=True,
                sortable=True,
            ),
            SimpleField(
                name="blob_url",
                type=SearchFieldDataType.String,
                filterable=False,
            ),
            SimpleField(
                name="last_modified",
                type=SearchFieldDataType.DateTimeOffset,
                filterable=True,
                sortable=True,
            ),
            # ── Content ───────────────────────────────────────────────────
            SearchableField(
                name="content",
                type=SearchFieldDataType.String,
                analyzer_name="en.microsoft",
            ),
            SearchableField(
                name="merged_content",
                type=SearchFieldDataType.String,
                analyzer_name="en.microsoft",
            ),
            # ── Document Intelligence extracted layout ─────────────────────
            SearchableField(
                name="layout_text",
                type=SearchFieldDataType.String,
                analyzer_name="en.microsoft",
            ),
            # ── OCR text from embedded images ─────────────────────────────
            SearchableField(
                name="image_text",
                type=SearchFieldDataType.String,
                analyzer_name="en.microsoft",
            ),
            # ── Structured extractions ────────────────────────────────────
            SimpleField(
                name="key_phrases",
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                filterable=True,
                facetable=True,
            ),
            SimpleField(
                name="entities",
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                filterable=True,
                facetable=True,
            ),
            # ── Chunked content (produced by SplitSkill) ──────────────────
            SimpleField(
                name="pages",
                type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                searchable=True,
            ),
            # ── Metadata ──────────────────────────────────────────────────
            SimpleField(
                name="file_size",
                type=SearchFieldDataType.Int64,
                filterable=True,
                sortable=True,
            ),
            SearchableField(
                name="title",
                type=SearchFieldDataType.String,
            ),
            SimpleField(
                name="author",
                type=SearchFieldDataType.String,
                filterable=True,
                facetable=True,
            ),
            SimpleField(
                name="page_count",
                type=SearchFieldDataType.Int32,
                filterable=True,
                sortable=True,
            ),
        ]

        # ── Vector field (only when Azure OpenAI is configured) ───────────
        if self.cfg.pipeline.enable_vector_search and self.cfg.openai.is_configured:
            fields.append(
                SearchField(
                    name="content_vector",
                    type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True,
                    vector_search_dimensions=self.cfg.openai.dimensions,
                    vector_search_profile_name="hnsw-profile",
                )
            )

        return fields

    def _build_vector_search(self) -> VectorSearch | None:
        if not (self.cfg.pipeline.enable_vector_search and self.cfg.openai.is_configured):
            return None
        return VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name="hnsw-algo")],
            profiles=[
                VectorSearchProfile(
                    name="hnsw-profile",
                    algorithm_configuration_name="hnsw-algo",
                )
            ],
        )

    def _build_semantic_search(self) -> SemanticSearch:
        return SemanticSearch(
            configurations=[
                SemanticConfiguration(
                    name="pdf-semantic",
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name="title"),
                        content_fields=[
                            SemanticField(field_name="merged_content"),
                            SemanticField(field_name="layout_text"),
                        ],
                        keywords_fields=[SemanticField(field_name="key_phrases")],
                    ),
                )
            ]
        )

    def create_index(self) -> None:
        """Create (or update) the search index.

        If the existing index has incompatible field definitions (e.g. an
        analyzer was added to a field that was previously unanalyzed), the old
        index is deleted and recreated from scratch.
        """
        from azure.core.exceptions import HttpResponseError

        index = SearchIndex(
            name=self.cfg.search.index_name,
            fields=self._build_fields(),
            vector_search=self._build_vector_search(),
            semantic_search=self._build_semantic_search(),
        )
        try:
            self._index_client.create_or_update_index(index)
        except HttpResponseError as exc:
            if "CannotChangeExistingField" in str(exc) or "cannot be changed" in str(exc):
                logger.warning(
                    "Index '%s' has incompatible field definitions – deleting and recreating.",
                    self.cfg.search.index_name,
                )
                self._index_client.delete_index(self.cfg.search.index_name)
                self._index_client.create_or_update_index(index)
            else:
                raise
        logger.info("Index '%s' ready.", self.cfg.search.index_name)

    # ── 3. Skillset ───────────────────────────────────────────────────────────

    def create_skillset(self) -> None:
        """
        Build the enrichment pipeline via REST API (required for
        DocumentIntelligenceLayoutSkill which needs the preview API version):

          /document/content
              │
          DocumentIntelligenceLayout  → layout_text
              │
          OcrSkill (images)           → image_text
              │
          MergeSkill                  → merged_content
              │
          KeyPhrase + EntityRecognition
              │
          SplitSkill                  → pages
              │
          AzureOpenAIEmbedding (opt)  → content_vector
        """
        skills: list[dict] = []

        # ── Document Intelligence Layout ──────────────────────────────────
        skills.append({
            "@odata.type": "#Microsoft.Skills.Util.DocumentIntelligenceLayoutSkill",
            "name": "doc-intelligence-layout",
            "description": "Extract text, tables, and layout from PDFs using Azure Document Intelligence",
            "context": "/document",
            "inputs": [{"name": "file_data", "source": "/document/file_data"}],
            "outputs": [{"name": "markdown_document", "targetName": "layout_text"}],
        })

        # ── OCR (for images embedded in PDFs) ─────────────────────────────
        skills.append({
            "@odata.type": "#Microsoft.Skills.Vision.OcrSkill",
            "name": "ocr-images",
            "description": "Run OCR on images embedded in PDF pages",
            "context": "/document/normalized_images/*",
            "defaultLanguageCode": "en",
            "detectOrientation": True,
            "inputs": [{"name": "image", "source": "/document/normalized_images/*"}],
            "outputs": [
                {"name": "text", "targetName": "image_text"},
                {"name": "layoutText", "targetName": "image_layout_text"},
            ],
        })

        # ── Merge content + OCR text ──────────────────────────────────────
        skills.append({
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
        })

        # ── Key Phrase Extraction ─────────────────────────────────────────
        skills.append({
            "@odata.type": "#Microsoft.Skills.Text.KeyPhraseExtractionSkill",
            "name": "key-phrases",
            "description": "Extract key phrases from merged content",
            "context": "/document",
            "defaultLanguageCode": "en",
            "inputs": [{"name": "text", "source": "/document/merged_content"}],
            "outputs": [{"name": "keyPhrases", "targetName": "key_phrases"}],
        })

        # ── Entity Recognition ────────────────────────────────────────────
        skills.append({
            "@odata.type": "#Microsoft.Skills.Text.V3.EntityRecognitionSkill",
            "name": "entity-recognition",
            "description": "Recognize entities (people, places, organizations)",
            "context": "/document",
            "categories": ["Person", "Location", "Organization", "DateTime", "URL"],
            "defaultLanguageCode": "en",
            "inputs": [{"name": "text", "source": "/document/merged_content"}],
            "outputs": [{"name": "namedEntities", "targetName": "entities_raw"}],
        })

        # ── Split into pages / chunks ─────────────────────────────────────
        skills.append({
            "@odata.type": "#Microsoft.Skills.Text.SplitSkill",
            "name": "split-pages",
            "description": "Split merged content into searchable chunks",
            "context": "/document",
            "textSplitMode": "pages",
            "maximumPageLength": self.cfg.pipeline.chunk_size,
            "pageOverlapLength": self.cfg.pipeline.chunk_overlap,
            "inputs": [{"name": "text", "source": "/document/merged_content"}],
            "outputs": [{"name": "textItems", "targetName": "pages"}],
        })

        # ── Azure OpenAI Embeddings (optional) ────────────────────────────
        if self.cfg.pipeline.enable_vector_search and self.cfg.openai.is_configured:
            skills.append({
                "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
                "name": "openai-embeddings",
                "description": "Generate vector embeddings for semantic/vector search",
                "context": "/document",
                "resourceUri": self.cfg.openai.endpoint.rstrip("/"),
                "apiKey": self.cfg.openai.key,
                "deploymentId": self.cfg.openai.embedding_deployment,
                "dimensions": self.cfg.openai.dimensions,
                "modelName": self.cfg.openai.embedding_deployment,
                "inputs": [{"name": "text", "source": "/document/merged_content"}],
                "outputs": [{"name": "embedding", "targetName": "content_vector"}],
            })

        body = {
            "name": self.cfg.search.skillset_name,
            "description": "Multi-modal PDF enrichment: layout, OCR, NLP, chunking",
            "skills": skills,
        }

        url = (
            f"{self.cfg.search.endpoint.rstrip('/')}/skillsets"
            f"/{self.cfg.search.skillset_name}"
            f"?api-version={self.API_VERSION}"
        )
        resp = requests.put(
            url,
            headers=_rest_headers(self.cfg.search.admin_key),
            json=body,
            timeout=60,
        )
        if not resp.ok:
            logger.error("Skillset PUT failed %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
        logger.info("Skillset '%s' ready.", self.cfg.search.skillset_name)

    # ── 4. Indexer ────────────────────────────────────────────────────────────

    def create_indexer(self) -> None:
        """
        Create (or update) the blob indexer and kick off the first run.

        Key settings
        ────────────
        • generateNormalizedImages → extract images from PDFs for OCR
        • imageAction              → generateNormalizedImages (inline + referenced)
        • dataToExtract            → contentAndMetadata
        • parsingMode              → default (per-blob document)
        """
        params = IndexingParameters(
            configuration=IndexingParametersConfiguration(
                data_to_extract=BlobIndexerDataToExtract.CONTENT_AND_METADATA,
                image_action=BlobIndexerImageAction.GENERATE_NORMALIZED_IMAGES,
                parsing_mode="default",  # one document per PDF
                pdf_text_rotation_algorithm="detectAngles",
                # Allow up to 256 MB per blob
                execution_environment="standard",
            ),
            max_failed_items=-1,         # don't stop on individual failures
            max_failed_items_per_batch=-1,
        )

        # Field mappings: blob metadata → index fields
        field_mappings = [
            {"sourceFieldName": "metadata_storage_path",    "targetFieldName": "id",
             "mappingFunction": {"name": "base64Encode"}},
            {"sourceFieldName": "metadata_storage_name",    "targetFieldName": "blob_name"},
            {"sourceFieldName": "metadata_storage_path",    "targetFieldName": "blob_url"},
            {"sourceFieldName": "metadata_storage_last_modified", "targetFieldName": "last_modified"},
            {"sourceFieldName": "metadata_storage_size",    "targetFieldName": "file_size"},
            {"sourceFieldName": "metadata_title",           "targetFieldName": "title"},
            {"sourceFieldName": "metadata_author",          "targetFieldName": "author"},
            {"sourceFieldName": "metadata_page_count",      "targetFieldName": "page_count"},
        ]

        # Output field mappings: skill outputs → index fields
        output_field_mappings = [
            {"sourceFieldName": "/document/layout_text",    "targetFieldName": "layout_text"},
            {"sourceFieldName": "/document/merged_content", "targetFieldName": "merged_content"},
            {"sourceFieldName": "/document/key_phrases",    "targetFieldName": "key_phrases"},
            {"sourceFieldName": "/document/pages",          "targetFieldName": "pages"},
        ]

        if self.cfg.pipeline.enable_vector_search and self.cfg.openai.is_configured:
            output_field_mappings.append(
                {"sourceFieldName": "/document/content_vector", "targetFieldName": "content_vector"}
            )

        # Use REST API for the indexer to pass raw field mapping dicts
        body = {
            "name": self.cfg.search.indexer_name,
            "description": "Indexes PDFs from Azure Blob Storage with rich content extraction",
            "dataSourceName": self.cfg.search.datasource_name,
            "skillsetName": self.cfg.search.skillset_name,
            "targetIndexName": self.cfg.search.index_name,
            "schedule": {"interval": "PT2H"},   # reindex every 2 hours for new/changed blobs
            "parameters": {
                "configuration": {
                    "dataToExtract": "contentAndMetadata",
                    "imageAction": "generateNormalizedImages",
                    "parsingMode": "default",
                    "pdfTextRotationAlgorithm": "detectAngles",
                }
            },
            "fieldMappings": field_mappings,
            "outputFieldMappings": output_field_mappings,
        }

        url = (
            f"{self.cfg.search.endpoint.rstrip('/')}/indexers"
            f"/{self.cfg.search.indexer_name}"
            f"?api-version={self.API_VERSION}"
        )
        resp = requests.put(
            url,
            headers=_rest_headers(self.cfg.search.admin_key),
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        logger.info("Indexer '%s' created.", self.cfg.search.indexer_name)

    # ── Run / Status ──────────────────────────────────────────────────────────

    def run_indexer(self) -> None:
        """Trigger an immediate indexer run."""
        url = (
            f"{self.cfg.search.endpoint.rstrip('/')}/indexers"
            f"/{self.cfg.search.indexer_name}/run"
            f"?api-version={self.API_VERSION}"
        )
        resp = requests.post(
            url, headers=_rest_headers(self.cfg.search.admin_key), timeout=30
        )
        if resp.status_code == 202:
            logger.info("Indexer run triggered.")
        else:
            resp.raise_for_status()

    def wait_for_indexer(self, poll_interval: int = 10, timeout: int = 600) -> dict:
        """
        Poll the indexer status until it finishes or times out.

        Returns the final status dict.
        """
        url = (
            f"{self.cfg.search.endpoint.rstrip('/')}/indexers"
            f"/{self.cfg.search.indexer_name}/status"
            f"?api-version={self.API_VERSION}"
        )
        headers = _rest_headers(self.cfg.search.admin_key)
        deadline = time.time() + timeout

        while time.time() < deadline:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            status = resp.json()
            last_run = status.get("lastResult") or {}
            run_status = last_run.get("status", "unknown")
            logger.info("Indexer status: %s", run_status)

            if run_status in ("success", "transientFailure", "persistentFailure"):
                return last_run

            time.sleep(poll_interval)

        raise TimeoutError(
            f"Indexer did not complete within {timeout}s. "
            "Check the Azure portal for details."
        )

    def get_indexer_status(self) -> dict:
        """Return the raw indexer status JSON."""
        url = (
            f"{self.cfg.search.endpoint.rstrip('/')}/indexers"
            f"/{self.cfg.search.indexer_name}/status"
            f"?api-version={self.API_VERSION}"
        )
        resp = requests.get(url, headers=_rest_headers(self.cfg.search.admin_key), timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Teardown ──────────────────────────────────────────────────────────────

    def teardown(self) -> None:
        """Delete all search resources (useful for clean re-provisioning)."""
        for name, client, method in [
            (self.cfg.search.indexer_name,    self._indexer_client, "delete_indexer"),
            (self.cfg.search.skillset_name,   self._indexer_client, "delete_skillset"),
            (self.cfg.search.datasource_name, self._indexer_client, "delete_data_source_connection"),
            (self.cfg.search.index_name,      self._index_client,   "delete_index"),
        ]:
            try:
                getattr(client, method)(name)
                logger.info("Deleted: %s", name)
            except Exception as exc:
                logger.debug("Could not delete %s: %s", name, exc)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    from config import load_config
    cfg = load_config()
    setup = SearchPipelineSetup(cfg)
    setup.create_data_source(cfg.blob.connection_string)  # also accepts a SAS URL
    setup.create_index()
    setup.create_skillset()
    setup.create_indexer()
    setup.run_indexer()
    status = setup.wait_for_indexer()