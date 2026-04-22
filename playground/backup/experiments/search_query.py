"""
search_query.py – Query the Azure AI Search index.

Supports four search modes:
  • full_text   – BM25 keyword search with filters + facets
  • semantic    – Semantic re-ranking with captions & answers
  • vector      – Pure vector (cosine) search via Azure OpenAI embeddings
  • hybrid      – Vector + BM25 combined (best of both worlds)

Usage
─────
    from src.search_query import SearchClient
    from src.config import load_config

    cfg = load_config()
    client = SearchClient(cfg)

    results = client.search("quarterly revenue by region", mode="hybrid", top=5)
    for r in results:
        print(r["blob_name"], r["@search.score"])
        print(r.get("@search.captions"))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient as _AzureSearchClient
from azure.search.documents.models import (
    QueryType,
    QueryAnswerType,
    QueryCaptionType,
    VectorizedQuery,
    SemanticErrorMode,
)

from config import AppConfig

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

logger = logging.getLogger(__name__)

SearchMode = Literal["full_text", "semantic", "vector", "hybrid"]


@dataclass
class SearchResult:
    """Structured wrapper around a single search hit."""

    id: str
    blob_name: str
    blob_url: str
    score: float
    reranker_score: float | None = None
    content: str = ""
    merged_content: str = ""
    layout_text: str = ""
    key_phrases: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    captions: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    title: str = ""
    author: str = ""
    page_count: int | None = None

    @classmethod
    def from_raw(cls, raw: dict) -> "SearchResult":
        captions = []
        if raw.get("@search.captions"):
            captions = [
                c.get("text", "") or c.get("highlights", "")
                for c in raw["@search.captions"]
            ]

        answers = []
        if raw.get("@search.answers"):
            answers = [
                a.get("text", "") or a.get("highlights", "")
                for a in raw["@search.answers"]
            ]

        return cls(
            id=raw.get("id", ""),
            blob_name=raw.get("blob_name", ""),
            blob_url=raw.get("blob_url", ""),
            score=raw.get("@search.score", 0.0),
            reranker_score=raw.get("@search.reranker_score"),
            content=raw.get("content", ""),
            merged_content=raw.get("merged_content", ""),
            layout_text=raw.get("layout_text", ""),
            key_phrases=raw.get("key_phrases") or [],
            entities=raw.get("entities") or [],
            captions=captions,
            answers=answers,
            title=raw.get("title", ""),
            author=raw.get("author", ""),
            page_count=raw.get("page_count"),
        )


class PDFSearchClient:
    """
    High-level search client for the PDF index.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._client = _AzureSearchClient(
            endpoint=cfg.search.endpoint,
            index_name=cfg.search.index_name,
            credential=AzureKeyCredential(cfg.search.admin_key),
        )

    # ── Embedding helper ──────────────────────────────────────────────────────

    def _embed(self, text: str) -> list[float]:
        """Generate an embedding vector using Azure OpenAI."""
        import requests

        if not self.cfg.openai.is_configured:
            raise RuntimeError(
                "Azure OpenAI is not configured. "
                "Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY in .env."
            )

        url = (
            f"{self.cfg.openai.endpoint.rstrip('/')}/openai/deployments"
            f"/{self.cfg.openai.embedding_deployment}/embeddings"
            f"?api-version=2024-02-01"
        )
        resp = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "api-key": self.cfg.openai.key,
            },
            json={"input": text, "dimensions": self.cfg.openai.dimensions},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    # ── Search methods ────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        mode: SearchMode = "semantic",
        top: int = 5,
        filter_expr: str | None = None,
        select_fields: list[str] | None = None,
        highlight_fields: list[str] | None = None,
    ) -> list[SearchResult]:
        """
        Run a search and return structured results.

        Parameters
        ----------
        query         : search query string
        mode          : one of full_text | semantic | vector | hybrid
        top           : number of results to return
        filter_expr   : OData filter expression, e.g. "author eq 'Alice'"
        select_fields : which fields to include in results (None = all)
        highlight_fields : fields to highlight in results

        Returns
        -------
        List of SearchResult objects sorted by relevance.
        """
        dispatch = {
            "full_text": self._full_text_search,
            "semantic":  self._semantic_search,
            "vector":    self._vector_search,
            "hybrid":    self._hybrid_search,
        }

        if mode not in dispatch:
            raise ValueError(f"Unknown search mode '{mode}'. Choose from: {list(dispatch)}")

        logger.info("[%s] query=%r  top=%d", mode.upper(), query, top)
        return dispatch[mode](
            query=query,
            top=top,
            filter_expr=filter_expr,
            select_fields=select_fields,
            highlight_fields=highlight_fields,
        )

    def _default_fields(self) -> list[str]:
        return [
            "captions"
            # "id", "blob_name", "blob_url", "title", "author",
            # "page_count", "key_phrases", "entities",
            # "merged_content", "layout_text",
        ]
       # return [
       #     "id", "blob_name", "blob_url", "title", "author",
       #      "page_count", "key_phrases", "entities",
       #      "merged_content", "layout_text",
       #  ]

    def _full_text_search(
        self, query: str, top: int, filter_expr, select_fields, highlight_fields
    ) -> list[SearchResult]:
        results = self._client.search(
            search_text=query,
            query_type=QueryType.FULL,
            search_fields=["merged_content", "layout_text", "title", "key_phrases"],
            select=select_fields or self._default_fields(),
            filter=filter_expr,
            highlight_fields=",".join(highlight_fields or ["merged_content", "layout_text"]),
            highlight_pre_tag="<mark>",
            highlight_post_tag="</mark>",
            top=top,
            include_total_count=True,
        )
        hits = list(results)
        logger.info("Full-text search returned %d results.", len(hits))
        return [SearchResult.from_raw(dict(h)) for h in hits]

    def _semantic_search(
        self, query: str, top: int, filter_expr, select_fields, highlight_fields
    ) -> list[SearchResult]:
        results = self._client.search(
            search_text=query,
            query_type=QueryType.SEMANTIC,
            semantic_configuration_name="pdf-semantic",
            query_answer=QueryAnswerType.EXTRACTIVE,
            query_answer_count=3,
            query_caption=QueryCaptionType.EXTRACTIVE,
            query_caption_highlight_enabled=True,
            semantic_error_mode=SemanticErrorMode.PARTIAL,
            select=select_fields or self._default_fields(),
            filter=filter_expr,
            top=top,
        )
        hits = list(results)
        logger.info("Semantic search returned %d results.", len(hits))
        return [SearchResult.from_raw(dict(h)) for h in hits]

    def _vector_search(
        self, query: str, top: int, filter_expr, select_fields, highlight_fields
    ) -> list[SearchResult]:
        vector = self._embed(query)
        vectorized = VectorizedQuery(
            vector=vector,
            k_nearest_neighbors=top,
            fields="content_vector",
        )
        results = self._client.search(
            search_text=None,
            vector_queries=[vectorized],
            select=select_fields or self._default_fields(),
            filter=filter_expr,
            top=top,
        )
        hits = list(results)
        logger.info("Vector search returned %d results.", len(hits))
        return [SearchResult.from_raw(dict(h)) for h in hits]

    def _hybrid_search(
        self, query: str, top: int, filter_expr, select_fields, highlight_fields
    ) -> list[SearchResult]:
        vector = self._embed(query)
        vectorized = VectorizedQuery(
            vector=vector,
            k_nearest_neighbors=top,
            fields="content_vector",
        )
        results = self._client.search(
            search_text=query,
            vector_queries=[vectorized],
            query_type=QueryType.SEMANTIC,
            semantic_configuration_name="pdf-semantic",
            query_answer=QueryAnswerType.EXTRACTIVE,
            query_answer_count=3,
            query_caption=QueryCaptionType.EXTRACTIVE,
            select=select_fields or self._default_fields(),
            filter=filter_expr,
            top=top,
        )
        hits = list(results)
        logger.info("Hybrid search returned %d results.", len(hits))
        return [SearchResult.from_raw(dict(h)) for h in hits]

    # ── Convenience helpers ───────────────────────────────────────────────────

    def get_document(self, doc_id: str) -> dict | None:
        """Fetch a single document by its index key."""
        try:
            return dict(self._client.get_document(key=doc_id))
        except Exception:
            return None

    def suggest(self, partial_query: str, suggester_name: str = "sg", top: int = 5) -> list[str]:
        """
        Return autocomplete suggestions for a partial query.
        Note: a suggester must be defined in the index (not added here by default).
        """
        results = self._client.suggest(
            search_text=partial_query,
            suggester_name=suggester_name,
            top=top,
        )
        return [r["@search.text"] for r in results]

    def count(self) -> int:
        """Return the total number of documents in the index."""
        results = self._client.search(search_text="*", include_total_count=True, top=0)
        return results.get_count() or 0

    def facets(self, facet_fields: list[str], filter_expr: str | None = None) -> dict[str, list]:
        """
        Return facet counts for one or more fields.

        Example
        -------
            client.facets(["author", "key_phrases"])
        """
        facet_params = [f"{f},count:20" for f in facet_fields]
        results = self._client.search(
            search_text="*",
            facets=facet_params,
            filter=filter_expr,
            top=0,
        )
        return results.get_facets() or {}

if __name__ == "__main__":
    from config import load_config
    cfg = load_config()

    # Search
    client = PDFSearchClient(cfg)
    results = client.search("quarterly revenue", mode="semantic", top=5)
    for r in results:
        print(r.blob_name, r.score)
        print(r.captions)