"""
app.py - Streamlit UI for the Azure Multimodal RAG pipeline.

In production this UI calls the FastAPI backend (FASTAPI_URL env var) instead
of hitting Azure services directly. The FastAPI service handles auth and
proxies blob data.

Features:
  - Full-text search bar with configurable top-N results
  - Per-result card: relevance score, file name, title
  - First page rendered as an image (requires PyMuPDF - pip install pymupdf)
  - Structured layout rendered as Markdown (tables, headings from Doc Intelligence)
  - Raw text chunks (pages) shown in an expander
  - Direct link to open the source document via the backend blob proxy

Run locally (pointing at local FastAPI):
    FASTAPI_URL=http://localhost:8000 streamlit run azure-mmr/app.py
"""

import io
import json
import os

import requests
import streamlit as st

# FASTAPI_URL is the only external dependency in production.
# Defaults to localhost for local development.
FASTAPI_URL: str = os.environ.get("FASTAPI_URL", "http://localhost:8000").rstrip("/")
INDEX_NAME: str = os.environ.get("AZURE_SEARCH_INDEX_NAME", "")

# PyMuPDF (optional - PDF to image preview)
try:
    import fitz  # PyMuPDF
    _PYMUPDF_AVAILABLE = True
except ImportError:
    _PYMUPDF_AVAILABLE = False


# Helpers

def _blob_proxy_url(blob_name: str) -> str:
    """Return the FastAPI blob-proxy URL for a given blob."""
    return f"{FASTAPI_URL}/blob-proxy/{blob_name}"


@st.cache_data(show_spinner=False)
def _fetch_blob_bytes(blob_name: str) -> bytes | None:
    """Download a blob via the FastAPI proxy. Cached per blob_name."""
    try:
        resp = requests.get(_blob_proxy_url(blob_name), timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        st.warning(f"Could not fetch blob '{blob_name}': {exc}")
        return None


def _render_pdf_page(pdf_bytes: bytes, page_index: int = 0, dpi: int = 150) -> bytes | None:
    """Render a single PDF page to PNG bytes using PyMuPDF."""
    if not _PYMUPDF_AVAILABLE:
        return None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if page_index >= len(doc):
            page_index = 0
        page = doc[page_index]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")
    except Exception:
        return None


def _parse_layout_text(raw: str) -> str:
    """Unwrap layout_text (JSON array of {content} objects) to plain Markdown."""
    if not raw:
        return ""
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return "\n\n".join(
                item.get("content", "") for item in items if isinstance(item, dict)
            )
    except (json.JSONDecodeError, TypeError):
        pass
    return raw


def _score_badge(score: float) -> str:
    """Return a colour-coded score label as inline HTML."""
    colour = "green" if score >= 3.0 else "orange" if score >= 1.5 else "red"
    return (
        f'<span style="background:{colour};color:white;padding:2px 8px;'
        f'border-radius:4px;font-size:0.8rem">score {score:.3f}</span>'
    )


def _run_search(query: str, top: int) -> list[dict]:
    """Call the FastAPI /search endpoint and return result list."""
    try:
        resp = requests.post(
            f"{FASTAPI_URL}/search",
            json={"query": query, "top": top},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as exc:
        st.error(f"Search failed: {exc}")
        return []


# Page config
st.set_page_config(
    page_title="Azure MMR Search",
    page_icon="🔍",
    layout="wide",
)

# Sidebar
with st.sidebar:
    st.image(
        "https://upload.wikimedia.org/wikipedia/commons/thumb/f/fa/Microsoft_Azure.svg/150px-Microsoft_Azure.svg.png",
        width=40,
    )
    st.title("Azure MMR Search")
    if INDEX_NAME:
        st.caption(f"Index: `{INDEX_NAME}`")
    st.divider()

    top_n = st.slider("Max results", min_value=1, max_value=20, value=5)
    show_layout = st.toggle("Show layout (tables/headings)", value=True)
    show_chunks = st.toggle("Show text chunks", value=False)
    show_preview = st.toggle("Show document preview", value=True)
    preview_page = st.number_input("Preview page #", min_value=1, value=1, step=1) - 1

    st.divider()
    if not _PYMUPDF_AVAILABLE:
        st.warning("PyMuPDF not installed.\nDocument previews disabled.\n\n`pip install pymupdf`")
    else:
        st.success("PyMuPDF ready - PDF previews enabled.")
    st.caption(f"Backend: `{FASTAPI_URL}`")

# Main area
st.header("Multimodal Document Search")
st.caption("Searches across extracted text, layout structure, and OCR content from PDFs in Azure Blob Storage.")

query = st.text_input(
    "Search",
    placeholder='e.g. "assembly revision" or a part number like "PN-78169"',
    label_visibility="collapsed",
)

run_search = st.button("Search", type="primary", use_container_width=False)

if not query:
    st.info("Enter a search query above and click **Search**.")
    st.stop()

if not run_search and "last_query" not in st.session_state:
    st.stop()

# Execute search
if run_search or st.session_state.get("last_query") != query:
    st.session_state["last_query"] = query
    with st.spinner("Searching..."):
        hits = _run_search(query, top_n)
    st.session_state["hits"] = hits

hits = st.session_state.get("hits", [])

if not hits:
    st.warning("No results found. Try a different query or check that the index has documents.")
    st.stop()

st.markdown(f"**{len(hits)} result(s)** for *{query}*")
st.divider()

# Render results
for i, hit in enumerate(hits, 1):
    blob_name = hit["file"]
    title = hit["title"] or blob_name
    score = hit["score"]
    proxy_url = _blob_proxy_url(blob_name) if blob_name else ""

    with st.container():
        col_info, col_link = st.columns([5, 1])
        with col_info:
            st.markdown(
                f"**{i}. {title}** &nbsp; {_score_badge(score)} &nbsp; "
                f"<span style='color:grey;font-size:0.8rem'>{blob_name}</span>",
                unsafe_allow_html=True,
            )
        with col_link:
            if proxy_url:
                st.markdown(
                    f'<a href="{proxy_url}" target="_blank">Open PDF</a>',
                    unsafe_allow_html=True,
                )

        st.markdown(f"> {hit['snippet']}...")

        if show_preview and blob_name and _PYMUPDF_AVAILABLE:
            pdf_bytes = _fetch_blob_bytes(blob_name)
            if pdf_bytes:
                png_bytes = _render_pdf_page(pdf_bytes, page_index=preview_page)
                if png_bytes:
                    st.image(io.BytesIO(png_bytes), caption=f"Page {preview_page + 1}", use_container_width=True)

        if show_layout and hit.get("layout_text"):
            with st.expander("Layout (tables / headings)"):
                st.markdown(_parse_layout_text(hit["layout_text"]))

        if show_chunks and hit.get("pages"):
            with st.expander("Text chunks"):
                for j, chunk in enumerate(hit["pages"], 1):
                    st.text_area(f"Chunk {j}", chunk, height=120, key=f"chunk_{i}_{j}")

        st.divider()