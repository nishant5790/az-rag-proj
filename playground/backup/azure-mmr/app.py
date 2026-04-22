"""
app.py – Streamlit UI for the Azure Multimodal RAG pipeline.

Features:
  • Full-text search bar with configurable top-N results
  • Per-result card: relevance score · file name · title
  • First page rendered as an image (requires PyMuPDF — pip install pymupdf)
  • Structured layout rendered as Markdown (tables, headings from Doc Intelligence)
  • Raw text chunks (pages) shown in an expander
  • Direct link to open the source document via SAS URL

Run from the project root:
    streamlit run azure-mmr/app.py
"""

import io
import json
import os
import sys

import requests
import streamlit as st
from dotenv import load_dotenv, find_dotenv

# ── Path setup ────────────────────────────────────────────────────────────────
# Allow imports from this package when launched from the project root
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

load_dotenv(find_dotenv())

# ── Truststore (corporate SSL) ────────────────────────────────────────────────
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import config as cfg  # noqa: E402  (after truststore)
from search import MMRSearch  # noqa: E402

# ── PyMuPDF (optional — PDF → image) ─────────────────────────────────────────
try:
    import fitz  # PyMuPDF
    _PYMUPDF_AVAILABLE = True
except ImportError:
    _PYMUPDF_AVAILABLE = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _blob_sas_url(blob_name: str) -> str:
    """Build a direct SAS URL for a single blob in the container."""
    token = cfg.BLOB_SAS_TOKEN.lstrip("?")
    return (
        f"https://{cfg.STORAGE_ACCOUNT}.blob.core.windows.net"
        f"/{cfg.CONTAINER}/{blob_name}?{token}"
    )


@st.cache_data(show_spinner=False)
def _fetch_blob_bytes(blob_name: str) -> bytes | None:
    """Download a blob and return its raw bytes. Cached per blob_name."""
    try:
        url = _blob_sas_url(blob_name)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        st.warning(f"Could not fetch blob '{blob_name}': {exc}")
        return None


def _render_pdf_page(pdf_bytes: bytes, page_index: int = 0, dpi: int = 150) -> bytes | None:
    """
    Render a single PDF page to a PNG using PyMuPDF.
    Returns PNG bytes or None if PyMuPDF is not available.
    """
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
    """
    layout_text is stored as a JSON array of objects with a 'content' key
    containing Markdown.  Unwrap and join for display.
    """
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
    return raw  # fallback: return as-is if already plain text


def _score_badge(score: float) -> str:
    """Return a colour-coded score label."""
    if score >= 3.0:
        colour = "green"
    elif score >= 1.5:
        colour = "orange"
    else:
        colour = "red"
    return f'<span style="background:{colour};color:white;padding:2px 8px;border-radius:4px;font-size:0.8rem">score {score:.3f}</span>'


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Azure MMR Search",
    page_icon="🔍",
    layout="wide",
)

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.image(
        "https://upload.wikimedia.org/wikipedia/commons/thumb/f/fa/Microsoft_Azure.svg/150px-Microsoft_Azure.svg.png",
        width=40,
    )
    st.title("Azure MMR Search")
    st.caption(f"Index: `{cfg.INDEX_NAME}`")
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
        st.success("PyMuPDF ready — PDF previews enabled.")

# ── Main area ─────────────────────────────────────────────────────────────────

st.header("🔍 Multimodal Document Search")
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

# ── Execute search ────────────────────────────────────────────────────────────

if run_search or st.session_state.get("last_query") != query:
    st.session_state["last_query"] = query
    with st.spinner("Searching…"):
        searcher = MMRSearch()
        hits = searcher.search(query, top=top_n)
    st.session_state["hits"] = hits

hits = st.session_state.get("hits", [])

if not hits:
    st.warning("No results found. Try a different query or check that the index has documents.")
    st.stop()

st.markdown(f"**{len(hits)} result(s)** for *{query}*")
st.divider()

# ── Render results ────────────────────────────────────────────────────────────

for i, hit in enumerate(hits, 1):
    blob_name = hit["file"]
    title = hit["title"] or blob_name
    score = hit["score"]
    sas_url = _blob_sas_url(blob_name) if blob_name else ""

    with st.container():
        # Header row
        col_meta, col_link = st.columns([8, 2])
        with col_meta:
            st.markdown(
                f"### {i}. {title} &nbsp; {_score_badge(score)}",
                unsafe_allow_html=True,
            )
            st.caption(f"📄 `{blob_name}`")
        with col_link:
            if sas_url:
                st.link_button("Open document ↗", sas_url, use_container_width=True)

        # Two-column layout: preview | content
        col_img, col_text = st.columns([2, 3], gap="large")

        with col_img:
            if show_preview and blob_name:
                if _PYMUPDF_AVAILABLE:
                    with st.spinner("Loading preview…"):
                        pdf_bytes = _fetch_blob_bytes(blob_name)
                    if pdf_bytes:
                        png = _render_pdf_page(pdf_bytes, page_index=preview_page)
                        if png:
                            st.image(
                                io.BytesIO(png),
                                caption=f"Page {preview_page + 1}",
                                use_container_width=True,
                            )
                        else:
                            st.info("Could not render page.")
                    else:
                        st.info("Preview unavailable.")
                else:
                    st.info(
                        "Install **PyMuPDF** to see PDF previews.\n\n"
                        "```\npip install pymupdf\n```"
                    )

        with col_text:
            # Content snippet
            # st.markdown("**Content snippet**")
            # snippet = hit["snippet"].strip()
            # if snippet:
            #     st.text(snippet[:500])
            # else:
            #     st.caption("No text content available.")

            # Layout (Doc Intelligence markdown)
            if show_layout:
                layout_md = _parse_layout_text(hit["layout_text"])
                if layout_md:
                    with st.expander("📐 Layout (tables & structure)", expanded=True):
                        st.markdown(layout_md[:300])
                        st.markdown("....")
                        st.markdown(layout_md[-30:])

            # Raw text chunks
            if show_chunks and hit["pages"]:
                with st.expander(f"📄 Text chunks ({len(hit['pages'])} pages)"):
                    for j, chunk in enumerate(hit["pages"], 1):
                        st.markdown(f"**Chunk {j}**")
                        st.text(chunk[:3])
                        st.text('....')
                        st.text(chunk[-3:])
                        st.divider()

            # OCR image text (if present)
            if hit.get("image_text"):
                with st.expander("🖼 OCR image text"):
                    st.text(hit["image_text"][:800])

        st.divider()
