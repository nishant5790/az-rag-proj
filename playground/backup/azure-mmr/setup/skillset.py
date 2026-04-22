"""
setup/skillset.py – Build the AI enrichment skillset for the multimodal RAG pipeline.

The skillset chains five skills in sequence:

  ① DocumentIntelligenceLayoutSkill
        Extracts text, tables, and headings from PDFs, preserving document
        structure as Markdown.  Output → /document/layout_text

  ② OcrSkill
        Runs OCR on rasterised images embedded in each page.
        Output → /document/normalized_images/*/image_text

  ③ MergeSkill
        Inserts the OCR text back into the raw content stream at the correct
        positional offsets.  Output → /document/merged_content

  ④ SplitSkill
        Chunks merged_content into overlapping 2000-character pages.
        Output → /document/pages/*

  ⑤ AzureOpenAIEmbeddingSkill
        Vectorises merged_content via the ada-002 model (1536 dimensions).
        Output → /document/content_vector

Note: The skillset is created via the REST API directly because not all skill
types are fully supported by the Python SDK at this API version.
"""

import logging

import config as cfg
from utils.http import rest_put

logger = logging.getLogger(__name__)


def create_skillset() -> None:
    """Create or update the enrichment skillset via the REST API."""
    body = {
        "name": cfg.SKILLSET_NAME,
        "description": "Multimodal RAG: Doc Intelligence + OCR + Merge + Split + OpenAI Embeddings",
        "skills": [
            # ── ① Document Intelligence Layout ────────────────────────────────
            {
                "@odata.type": "#Microsoft.Skills.Util.DocumentIntelligenceLayoutSkill",
                "name": "doc-intelligence-layout",
                "description": "Extract text, tables, and layout structure from PDFs as Markdown",
                "context": "/document",
                "inputs": [{"name": "file_data", "source": "/document/file_data"}],
                "outputs": [{"name": "markdown_document", "targetName": "layout_text"}],
            },

            # ── ② OCR Skill ───────────────────────────────────────────────────
            {
                "@odata.type": "#Microsoft.Skills.Vision.OcrSkill",
                "name": "ocr-images",
                "description": "Extract text from images embedded in PDFs",
                "context": "/document/normalized_images/*",
                "defaultLanguageCode": "en",
                "detectOrientation": True,
                "inputs": [{"name": "image", "source": "/document/normalized_images/*"}],
                "outputs": [
                    {"name": "text", "targetName": "image_text"},
                    {"name": "layoutText", "targetName": "image_layout_text"},
                ],
            },

            # ── ③ Merge Skill ─────────────────────────────────────────────────
            {
                "@odata.type": "#Microsoft.Skills.Text.MergeSkill",
                "name": "merge-content",
                "description": "Merge raw document content with OCR text from embedded images",
                "context": "/document",
                "insertPreTag": " ",
                "insertPostTag": " ",
                "inputs": [
                    {"name": "text", "source": "/document/content"},
                    {"name": "itemsToInsert", "source": "/document/normalized_images/*/image_text"},
                    {"name": "offsets", "source": "/document/normalized_images/*/contentOffset"},
                ],
                "outputs": [{"name": "mergedText", "targetName": "merged_content"}],
            },

            # ── ④ Split Skill ─────────────────────────────────────────────────
            {
                "@odata.type": "#Microsoft.Skills.Text.SplitSkill",
                "name": "split-pages",
                "description": "Chunk merged content into overlapping pages for retrieval",
                "context": "/document",
                "textSplitMode": "pages",
                "maximumPageLength": 2000,
                "pageOverlapLength": 200,
                "inputs": [{"name": "text", "source": "/document/merged_content"}],
                "outputs": [{"name": "textItems", "targetName": "pages"}],
            },

            # ── ⑤ Azure OpenAI Embedding Skill ───────────────────────────────
            {
                "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
                "name": "openai-embeddings",
                "description": "Generate 1536-d vector embeddings via Azure OpenAI ada-002",
                "context": "/document",
                "resourceUri": cfg.OPENAI_ENDPOINT.rstrip("/"),
                "apiKey": cfg.OPENAI_KEY,
                "deploymentId": cfg.EMBEDDING_DEPLOYMENT,
                "modelName": cfg.EMBEDDING_MODEL,
                "inputs": [{"name": "text", "source": "/document/merged_content"}],
                "outputs": [{"name": "embedding", "targetName": "content_vector"}],
            },
        ],
    }

    rest_put(f"skillsets/{cfg.SKILLSET_NAME}", body)
    logger.info("Skillset '%s' ready.", cfg.SKILLSET_NAME)
