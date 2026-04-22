"""
simple_search.py – Create an Azure AI Search index over PDFs in blob storage, then query it.
"""

import os
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
    SearchIndexerDataSourceConnection,
    SearchIndexerDataContainer,
    SearchIndexer,
    IndexingParameters,
    IndexingParametersConfiguration,
)

load_dotenv()

ENDPOINT = os.environ["AZURE_SEARCH_SERVICE_ENDPOINT"]
ADMIN_KEY = os.environ["AZURE_SEARCH_ADMIN_KEY"]
INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX_NAME", "pdf-search-index")
BLOB_SAS_URL = os.environ["BLOB_SAS_URL"]  # full container SAS URL
CONTAINER = os.environ["AZURE_BLOB_CONTAINER_NAME"]

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

# ── 1. Index + Indexer creator ────────────────────────────────────────────────

class IndexCreator:
    """Creates a search index, blob data source, and indexer."""

    def __init__(
        self,
        endpoint: str = ENDPOINT,
        admin_key: str = ADMIN_KEY,
        index_name: str = INDEX_NAME,
    ):
        credential = AzureKeyCredential(admin_key)
        self.index_client = SearchIndexClient(endpoint, credential)
        self.indexer_client = SearchIndexerClient(endpoint, credential)
        self.index_name = index_name
        self.ds_name = f"{index_name}-ds"
        self.indexer_name = f"{index_name}-indexer"

    # ---- index ----
    def create_index(self) -> None:
        fields = [
            SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
            SearchableField(name="content", type=SearchFieldDataType.String),
            SimpleField(name="metadata_storage_path", type=SearchFieldDataType.String, filterable=True),
            SearchableField(name="metadata_storage_name", type=SearchFieldDataType.String, filterable=True),
        ]
        index = SearchIndex(name=self.index_name, fields=fields)
        self.index_client.create_or_update_index(index)
        print(f"Index '{self.index_name}' created/updated.")

    # ---- data source ----
    def create_datasource(self) -> None:
        # Use ContainerSharedAccessUri format for SAS-based access
        conn_str = f"ContainerSharedAccessUri={BLOB_SAS_URL}"
        ds = SearchIndexerDataSourceConnection(
            name=self.ds_name,
            type="azureblob",
            connection_string=conn_str,
            container=SearchIndexerDataContainer(name=CONTAINER),
        )
        self.indexer_client.create_or_update_data_source_connection(ds)
        print(f"Data source '{self.ds_name}' created/updated.")

    # ---- indexer ----
    def create_indexer(self) -> None:
        indexer = SearchIndexer(
            name=self.indexer_name,
            data_source_name=self.ds_name,
            target_index_name=self.index_name,
            parameters=IndexingParameters(
                configuration={"parsingMode": "default", "dataToExtract": "contentAndMetadata"}
            ),
        )
        self.indexer_client.create_or_update_indexer(indexer)
        print(f"Indexer '{self.indexer_name}' created/updated.")

    def run_indexer(self) -> None:
        self.indexer_client.run_indexer(self.indexer_name)
        print(f"Indexer '{self.indexer_name}' triggered.")

    def setup_all(self) -> None:
        """One-shot: create index + data source + indexer, then run."""
        self.create_index()
        self.create_datasource()
        self.create_indexer()
        self.run_indexer()


# ── 2. Search / query class ──────────────────────────────────────────────────

class SearchQuery:
    """Simple full-text search against the index."""

    def __init__(
        self,
        endpoint: str = ENDPOINT,
        admin_key: str = ADMIN_KEY,
        index_name: str = INDEX_NAME,
    ):
        credential = AzureKeyCredential(admin_key)
        self.client = SearchClient(endpoint, index_name, credential)

    def search(self, query: str, top: int = 5):
        results = self.client.search(search_text=query, top=top)
        hits = []
        for r in results:
            hits.append({
                "score": r["@search.score"],
                "file": r.get("metadata_storage_name", ""),
                "snippet": (r.get("content") or "")[:300],
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
    import sys

    if len(sys.argv) < 2 or sys.argv[1] == "setup":
        print(">> Setting up index + indexer ...")
        IndexCreator().setup_all()
        print("\nDone. The indexer is running; documents should appear shortly.")

    if len(sys.argv) >= 2 and sys.argv[1] == "query":
        q = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "summary"
        SearchQuery().print_results(q)
