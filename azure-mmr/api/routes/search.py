"""
api/routes/search.py - POST /search endpoint.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from search.client import MMRSearch

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000, description="Search query text")
    top: int = Field(default=5, ge=1, le=50, description="Maximum number of results")


class SearchResponse(BaseModel):
    results: list[dict]
    count: int


@router.post("/search", response_model=SearchResponse)
def do_search(req: SearchRequest) -> SearchResponse:
    """
    Run a BM25 full-text search against the Azure AI Search index.

    Returns ranked document hits with score, file name, title, snippet,
    layout text, image text, and page chunks.
    """
    try:
        searcher = MMRSearch()
        hits = searcher.search(req.query, req.top)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Search backend error: {exc}") from exc
    return SearchResponse(results=hits, count=len(hits))