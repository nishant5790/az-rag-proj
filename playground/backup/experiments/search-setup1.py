"""
search_setup.py – Provisions the full Azure AI Search pipeline.

Creates (or updates) in this order:
  1. Data Source  → points to the Azure Blob container via SAS URL
  2. Index        → defines the searchable schema
  3. Skillset     → enrichment chain (Doc Intelligence → OCR → Merge → Split
                    → optionally Azure OpenAI embeddings)
  4. Indexer      → wires data source + skillset + index together and runs it

All resources are idempotent: calling setup() on an existing deployment will
update them to the latest definition rather than error out.

────────────────────────────────────────────────────────────────────────────────
Blob Authentication – SAS URL
────────────────────────────────────────────────────────────────────────────────
Azure AI Search data sources accept a SAS-based connection string in the form:

    BlobEndpoint=https://<account>.blob.core.windows.net;
    SharedAccessSignature=<sas_token>

The SAS token must have at minimum:  Read (r) + List (l)  on the container.
Generate one in the Azure Portal → Storage account → Shared access signature,
or via the Azure CLI:

    az storage container generate-sas \\
        --account-name <name> \\
        --name <container> \\
        --permissions rl \\
        --expiry 2025-12-31 \\
        --https-only \\
        --output tsv

Pass the resulting token (without leading '?') as AZURE_BLOB_SAS_TOKEN in .env.
────────────────────────────────────────────────────────────────────────────────
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
    OcrSkill,
    MergeSkill,
    SplitSkill,
    TextSplitMode,
    DocumentIntelligenceLayoutSkill,
    EntityRecognitionSkill,
    KeyPhraseExtractionSkill,
)

from config import AppConfig

logger = logging.getLogger(__name__)
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


# ─── REST helpers (for resources not yet in the SDK) ──────────────────────────

def _rest_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "api-key": api_key,
    }


def _put(endpoint: str, api_key: str, path: str, body: dict) -> requests.Response:
    url = f"{endpoint.rstrip('/')}/{path}?api-version=2024-05-01-preview"
    resp = requests.put(url, headers=_rest_headers(api_key), json=body, timeout=60)
    resp.raise_for_status()
    return resp


class SearchPipelineSetup:
    """
    Provisions and manages the full Azure AI Search pipeline for PDF ingestion.
    """

    API_VERSION = "2024-05-01-preview"

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
    def _build_sas_connection_string(account_name: str, sas_token: str) -> str:
        """
        Build the connection string format that Azure AI Search expects when
        authenticating to Blob Storage via a SAS token.

        Format:
            BlobEndpoint=https://<account>.blob.core.windows.net;
            SharedAccessSignature=<token_without_leading_?>

        The SAS token must grant at minimum: Read (r) + List (l) on the container.
        """
        token = sas_token.lstrip("?")
        return (
            f"BlobEndpoint=https://{account_name}.blob.core.windows.net;"
            f"SharedAccessSignature={token}"
        )

    def create_data_source(
        self,
        sas_token: str | None = None,
        blob_prefix: str | None = None,
    ) -> None:
        """
        Create (or update) a Blob Storage data source that authenticates
        via a SAS token — no account key required.

        Parameters
        ----------
        sas_token   : SAS token string (without leading '?').
                      Falls back to AZURE_BLOB_SAS_TOKEN env var when omitted.
        blob_prefix : Optional virtual-folder prefix to limit which blobs the
                      indexer picks up, e.g. "invoices/" or "reports/2024/".
                      Leave None to index all blobs in the container.

        Raises
        ------
        ValueError  : When no SAS token can be resolved from args or env.

        Notes
        -----
        The connection string stored in Azure AI Search uses the format:

            BlobEndpoint=https://<account>.blob.core.windows.net;
            SharedAccessSignature=<token>

        This is different from the standard storage account connection string
        (which uses AccountName + AccountKey). Using SAS means the indexer
        only ever gets read + list access, not full account access.
        """
        import os
        resolved_token = sas_token or os.getenv("AZURE_BLOB_SAS_TOKEN") or ""
        if not resolved_token:
            raise ValueError(
                "No SAS token provided. Pass sas_token= or set "
                "AZURE_BLOB_SAS_TOKEN in your .env file.\n\n"
                "Generate one with:\n"
                "  az storage container generate-sas \\\n"
                "      --account-name <name> --name <container> \\\n"
                "      --permissions rl --expiry 2025-12-31 --https-only --output tsv"
            )

        conn_str = self._build_sas_connection_string(
            account_name=self.cfg.blob.account_name,
            sas_token=resolved_token,
        )

        data_source = SearchIndexerDataSourceConnection(
            name=self.cfg.search.datasource_name,
            type=SearchIndexerDataSourceType.AZURE_BLOB,
            connection_string=conn_str,
            container=SearchIndexerDataContainer(
                name=self.cfg.blob.container_name,
                # query acts as a blob-name prefix filter; None = all blobs
                query=blob_prefix,
            ),
            description=(
                f"PDF documents in {self.cfg.blob.container_name} "
                f"(auth: SAS token, read+list)"
            ),
        )
        self._indexer_client.create_or_update_data_source_connection(data_source)
        logger.info(
            "Data source '%s' ready  [account=%s  container=%s  prefix=%s].",
            self.cfg.search.datasource_name,
            self.cfg.blob.account_name,
            self.cfg.blob.container_name,
            blob_prefix or "*",
        )
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
        """Create (or update) the search index."""
        index = SearchIndex(
            name=self.cfg.search.index_name,
            fields=self._build_fields(),
            vector_search=self._build_vector_search(),
            semantic_search=self._build_semantic_search(),
        )
        self._index_client.create_or_update_index(index)
        logger.info("Index '%s' ready.", self.cfg.search.index_name)


    # ── 3. Skillset ───────────────────────────────────────────────────────────

    def create_skillset(self) -> None:
        """
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
        skills: list[Any] = []

        # ── Document Intelligence Layout ──────────────────────────────────
        skills.append(
            DocumentIntelligenceLayoutSkill(
                name="doc-intelligence-layout",
                description="Extract text, tables, and layout from PDFs using Azure Document Intelligence",
                context="/document",
                output_content_format="markdown",  # preserves table structure
                inputs=[
                    InputFieldMappingEntry(name="file_data", source="/document/file_data"),
                ],
                outputs=[
                    OutputFieldMappingEntry(
                        name="markdown_document",
                        target_name="layout_text",
                    ),
                ],
            )
        )

        # ── OCR (for images embedded in PDFs) ─────────────────────────────
        skills.append(
            OcrSkill(
                name="ocr-images",
                description="Run OCR on images embedded in PDF pages",
                context="/document/normalized_images/*",
                default_language_code="en",
                should_detect_orientation=True,
                inputs=[
                    InputFieldMappingEntry(name="image", source="/document/normalized_images/*"),
                ],
                outputs=[
                    OutputFieldMappingEntry(name="text", target_name="image_text"),
                    OutputFieldMappingEntry(
                        name="layoutText", target_name="image_layout_text"
                    ),
                ],
            )
        )

        # ── Merge content + OCR text ──────────────────────────────────────
        skills.append(
            MergeSkill(
                name="merge-content",
                description="Merge raw content with OCR text from images",
                context="/document",
                insert_pre_tag=" ",
                insert_post_tag=" ",
                inputs=[
                    InputFieldMappingEntry(name="text", source="/document/content"),
                    InputFieldMappingEntry(
                        name="itemsToInsert",
                        source="/document/normalized_images/*/image_text",
                    ),
                    InputFieldMappingEntry(
                        name="offsets",
                        source="/document/normalized_images/*/contentOffset",
                    ),
                ],
                outputs=[
                    OutputFieldMappingEntry(
                        name="mergedText", target_name="merged_content"
                    ),
                ],
            )
        )

        # ── Key Phrase Extraction ─────────────────────────────────────────
        skills.append(
            KeyPhraseExtractionSkill(
                name="key-phrases",
                description="Extract key phrases from merged content",
                context="/document",
                default_language_code="en",
                inputs=[
                    InputFieldMappingEntry(
                        name="text", source="/document/merged_content"
                    ),
                ],
                outputs=[
                    OutputFieldMappingEntry(
                        name="keyPhrases", target_name="key_phrases"
                    ),
                ],
            )
        )

        # ── Entity Recognition ────────────────────────────────────────────
        skills.append(
            EntityRecognitionSkill(
                name="entity-recognition",
                description="Recognize entities (people, places, organizations)",
                context="/document",
                categories=["Person", "Location", "Organization", "DateTime", "URL"],
                default_language_code="en",
                inputs=[
                    InputFieldMappingEntry(
                        name="text", source="/document/merged_content"
                    ),
                ],
                outputs=[
                    OutputFieldMappingEntry(
                        name="namedEntities", target_name="entities_raw"
                    ),
                ],
            )
        )

        # ── Split into pages / chunks ─────────────────────────────────────
        skills.append(
            SplitSkill(
                name="split-pages",
                description="Split merged content into searchable chunks",
                context="/document",
                text_split_mode=TextSplitMode.PAGES,
                maximum_page_length=self.cfg.pipeline.chunk_size,
                page_overlap_length=self.cfg.pipeline.chunk_overlap,
                inputs=[
                    InputFieldMappingEntry(
                        name="text", source="/document/merged_content"
                    ),
                ],
                outputs=[
                    OutputFieldMappingEntry(name="textItems", target_name="pages"),
                ],
            )
        )

        # ── Azure OpenAI Embeddings (optional) ────────────────────────────
        if self.cfg.pipeline.enable_vector_search and self.cfg.openai.is_configured:
            # Use REST API directly as the SDK model may vary
            embedding_skill = {
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
            }
            # Patch skillset via REST to include this raw dict
            # (injected in _rest_create_skillset below)
            self._pending_embedding_skill = embedding_skill
        else:
            self._pending_embedding_skill = None

        # Build SDK skillset (without the embedding skill – added via REST)
        skillset = SearchIndexerSkillset(
            name=self.cfg.search.skillset_name,
            description="Multi-modal PDF enrichment: layout, OCR, NLP, chunking",
            skills=skills,
            cognitive_services_account=self._build_cognitive_services_ref(),
        )

        if self._pending_embedding_skill:
            # Use REST API to include the embedding skill in the payload
            self._rest_create_skillset(skillset, self._pending_embedding_skill)
        else:
            self._indexer_client.create_or_update_skillset(skillset)

        logger.info("Skillset '%s' ready.", self.cfg.search.skillset_name)

    def _build_cognitive_services_ref(self) -> Any:
        """
        Return a CognitiveServicesAccountKey object pointing to our
        Document Intelligence resource (also used for OCR / NLP skills).
        """
        from azure.search.documents.indexes.models import CognitiveServicesAccountKey
        return CognitiveServicesAccountKey(
            key=self.cfg.doc_intelligence.key,
            description="Azure AI multi-service account",
        )

    def _rest_create_skillset(
        self, skillset: SearchIndexerSkillset, extra_skill: dict
    ) -> None:
        """Serialize skillset + inject extra skill dict, then PUT via REST."""
        # Serialize the SDK skillset to dict via its internal _serialize method
        body = skillset._serialize()  # type: ignore[attr-defined]
        body["skills"].append(extra_skill)

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
        resp.raise_for_status()

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
        # NOTE: author is intentionally excluded from metadata mappings
        field_mappings = [
            {"sourceFieldName": "metadata_storage_path",         "targetFieldName": "id",
             "mappingFunction": {"name": "base64Encode"}},
            {"sourceFieldName": "metadata_storage_name",         "targetFieldName": "blob_name"},
            {"sourceFieldName": "metadata_storage_path",         "targetFieldName": "blob_url"},
            {"sourceFieldName": "metadata_storage_last_modified","targetFieldName": "last_modified"},
            {"sourceFieldName": "metadata_storage_size",         "targetFieldName": "file_size"},
            {"sourceFieldName": "metadata_title",                "targetFieldName": "title"},
            {"sourceFieldName": "metadata_page_count",           "targetFieldName": "page_count"},
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
    from config import load_config
    cfg = load_config()
    setup = SearchPipelineSetup(cfg)
    setup.create_data_source()
    setup.create_index()
    setup.create_skillset()
    setup.create_indexer()
    setup.run_indexer()
    status = setup.wait_for_indexer()