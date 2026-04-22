"""
setup/index.py – Define and provision the Azure AI Search index schema.

The index includes:
  - Standard metadata fields (id, blob_name, blob_url, last_modified, title)
  - Searchable text fields (content, merged_content, layout_text, image_text, pages)
  - A 1536-d vector field (content_vector) backed by an HNSW algorithm profile
  - A semantic configuration prioritising merged_content and layout_text
"""

import logging

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SearchableField,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)

import config as cfg

logger = logging.getLogger(__name__)


def create_index() -> None:
    """Create or update the search index with vector + semantic configuration."""
    fields = [
        # ── Identity / metadata ───────────────────────────────────────────────
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="blob_name", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="blob_url", type=SearchFieldDataType.String, filterable=False),
        SimpleField(name="last_modified", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SearchableField(name="title", type=SearchFieldDataType.String),

        # ── Text content ──────────────────────────────────────────────────────
        SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="merged_content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="layout_text", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="image_text", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SimpleField(
            name="pages",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            searchable=True,
        ),

        # ── Vector field ──────────────────────────────────────────────────────
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=cfg.EMBEDDING_DIMS,
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

    credential = AzureKeyCredential(cfg.ADMIN_KEY)
    index_client = SearchIndexClient(cfg.ENDPOINT, credential)
    index = SearchIndex(
        name=cfg.INDEX_NAME,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
    )
    index_client.create_or_update_index(index)
    logger.info("Index '%s' ready.", cfg.INDEX_NAME)
