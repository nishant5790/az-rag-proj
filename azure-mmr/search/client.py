"""
search/client.py - MMRSearch: full-text search client for the multimodal RAG index.

Uses DefaultAzureCredential in production (Workload Identity) and falls back
to AzureKeyCredential when AZURE_SEARCH_ADMIN_KEY is set (local dev).
"""

import logging

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient

import config as cfg

logger = logging.getLogger(__name__)


def _search_credential():
    """Return the appropriate credential based on environment."""
    if cfg.ADMIN_KEY:
        return AzureKeyCredential(cfg.ADMIN_KEY)
    return cfg.CREDENTIAL


class MMRSearch:
    """
    Hybrid-ready search client for the multimodal RAG index.

    Executes BM25 full-text search and returns ranked results with a file
    name and a content snippet for each hit.
    """

    def __init__(self) -> None:
        self.client = SearchClient(cfg.ENDPOINT, cfg.INDEX_NAME, _search_credential())

    def search(self, query: str, top: int = 5) -> list[dict]:
        """
        Run a full-text search query.

        Args:
            query: Natural language search string.
            top:   Maximum number of results to return.

        Returns:
            List of result dicts with keys:
              score, file, blob_url, title, snippet,
              layout_text, image_text, pages
        """
        results = self.client.search(
            search_text=query,
            top=top,
            select=[
                "id", "blob_name", "blob_url", "title",
                "merged_content", "layout_text", "image_text", "pages",
            ],
        )
        hits = []
        for r in results:
            hits.append({
                "score": r["@search.score"],
                "file": r.get("blob_name", ""),
                "blob_url": r.get("blob_url", ""),
                "title": r.get("title") or r.get("blob_name", ""),
                "snippet": (r.get("merged_content") or r.get("content") or "")[:400],
                "layout_text": r.get("layout_text") or "",
                "image_text": r.get("image_text") or "",
                "pages": r.get("pages") or [],
            })
        return hits

    def print_results(self, query: str, top: int = 5) -> None:
        """Pretty-print search results to stdout."""
        separator = "=" * 60
        print(f"\n{separator}")
        print(f"Query: {query}")
        print(separator)
        for i, hit in enumerate(self.search(query, top), 1):
            print(f"\n--- Result {i} (score {hit['score']:.4f}) ---")
            print(f"File   : {hit['file']}")
            print(f"Snippet: {hit['snippet']}...")