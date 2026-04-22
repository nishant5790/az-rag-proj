# Azure PDF Search Pipeline

A production-grade Python pipeline that uploads PDFs to **Azure Blob Storage**,
enriches them with **Azure Document Intelligence** (text, tables, images, layout),
and indexes everything into **Azure AI Search** for full-text, semantic, and
optional vector search.

---

## Architecture

```
Local PDFs
    │
    ▼
┌─────────────────────────────────────────────┐
│          Azure Blob Storage                 │
│  Container: pdf-documents                   │
│  *.pdf files stored as blobs               │
└──────────────────┬──────────────────────────┘
                   │  (data source connection)
                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Azure AI Search – Indexer Pipeline                        │
│                                                                             │
│  Blob Storage Data Source                                                   │
│          │                                                                  │
│          ▼                                                                  │
│  ┌───────────────────────────────────────────────────────────────────────┐ │
│  │                         Skillset (Enrichment)                         │ │
│  │                                                                       │ │
│  │  1. DocumentIntelligenceLayoutSkill  ← extracts text + tables        │ │
│  │          │  (markdown with table structure preserved)                 │ │
│  │          ▼                                                             │ │
│  │  2. OcrSkill  ← reads text from embedded images in PDF               │ │
│  │          │                                                             │ │
│  │          ▼                                                             │ │
│  │  3. MergeSkill  ← combines raw content + OCR image text              │ │
│  │          │                                                             │ │
│  │          ▼                                                             │ │
│  │  4. KeyPhraseExtractionSkill  ← NLP keyword extraction               │ │
│  │  5. EntityRecognitionSkill    ← people, places, orgs, dates          │ │
│  │          │                                                             │ │
│  │          ▼                                                             │ │
│  │  6. SplitSkill  ← chunk merged content into ~2000 token pages        │ │
│  │          │                                                             │ │
│  │          ▼  (optional)                                                 │ │
│  │  7. AzureOpenAIEmbeddingSkill  ← vector embeddings for hybrid search │ │
│  └───────────────────────────────────────────────────────────────────────┘ │
│          │                                                                  │
│          ▼                                                                  │
│  ┌───────────────────────────────────────────────────────────────────────┐ │
│  │                       Search Index Schema                             │ │
│  │  id • blob_name • blob_url • title • author • page_count             │ │
│  │  content • merged_content • layout_text • image_text                 │ │
│  │  key_phrases • entities • pages[] • content_vector[]                 │ │
│  └───────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
    Search Client
    ┌─────────────────────────────────────────┐
    │  • Full-text (BM25 + filters + facets)  │
    │  • Semantic  (re-ranking + captions)    │
    │  • Vector    (cosine similarity)        │
    │  • Hybrid    (vector + BM25 + semantic) │
    └─────────────────────────────────────────┘
```

---

## Prerequisites

| Service | Purpose |
|---|---|
| Azure Storage Account | Store PDF blobs |
| Azure AI Search (Basic+) | Index & search |
| Azure Document Intelligence | Extract layout, tables, images |
| Azure OpenAI *(optional)* | Vector embeddings for semantic/hybrid |

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your Azure resource credentials
```

### 3. Run the full pipeline

```bash
# Upload a directory of PDFs and index them
python pipeline.py --pdf-dir ./my_pdfs --wait

# Upload a single PDF
python pipeline.py --pdf-file ./report.pdf --wait

# Provision Azure resources only (no upload)
python pipeline.py --setup-only

# Upload without provisioning (resources already exist)
python pipeline.py --upload-only --pdf-dir ./my_pdfs

# Just trigger the indexer (blobs already in container)
python pipeline.py --index-only --wait
```

### 4. Search

```bash
# Semantic search (best for natural language questions)
python scripts/search.py --query "What were the Q3 revenue figures?"

# Full-text keyword search
python scripts/search.py --query "machine learning" --mode full_text

# Filter by author
python scripts/search.py --query "budget" --filter "author eq 'Finance'"

# Interactive REPL
python scripts/search.py --interactive

# Show facets (top authors, keywords)
python scripts/search.py --facets
```

---

## Project Structure

```
azure-pdf-search/
├── .env.example          # Environment variable template
├── requirements.txt      # Python dependencies
├── pipeline.py           # ← Main entry point (run this)
├── src/
│   ├── config.py         # Centralised configuration
│   ├── blob_manager.py   # Azure Blob Storage operations
│   ├── search_setup.py   # Provisions index/skillset/indexer
│   └── search_query.py   # Search client (4 modes)
└── scripts/
    └── search.py         # Search CLI with rich output
```

---

## Programmatic Usage

```python
from src.config import load_config
from src.blob_manager import BlobManager
from src.search_setup import SearchPipelineSetup
from src.search_query import PDFSearchClient

cfg = load_config()

# Upload a PDF
blob = BlobManager(cfg.blob)
blob.ensure_container()
blob.upload_pdf("./report.pdf")

# Provision search resources
setup = SearchPipelineSetup(cfg)
setup.create_data_source(cfg.blob.connection_string)
setup.create_index()
setup.create_skillset()
setup.create_indexer()
setup.run_indexer()
status = setup.wait_for_indexer()

# Search
client = PDFSearchClient(cfg)
results = client.search("quarterly revenue", mode="semantic", top=5)
for r in results:
    print(r.blob_name, r.score)
    print(r.captions)
```

---

## Search Modes

| Mode | When to use |
|---|---|
| `full_text` | Exact keyword matching, filters, faceting |
| `semantic` | Natural language questions, best relevance |
| `vector` | Conceptual similarity without keyword overlap |
| `hybrid` | Best overall relevance (requires Azure OpenAI) |

---

## Notes

- **Indexer schedule**: set to every 2 hours by default — new blobs are picked
  up automatically without re-running the pipeline.
- **Tables**: The `DocumentIntelligenceLayoutSkill` outputs Markdown, which
  preserves table structure in the `layout_text` field.
- **Images**: `OcrSkill` runs on every normalized image extracted from the PDF
  and the text is merged into `merged_content`.
- **Vector search**: disabled by default. Set `ENABLE_VECTOR_SEARCH=true` and
  configure Azure OpenAI credentials to enable it.
- Re-running `pipeline.py` is safe — all `create_or_update` calls are
  idempotent.
