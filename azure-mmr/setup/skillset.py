"""
setup/skillset.py - Build the AI enrichment skillset for the multimodal RAG pipeline.

In production the AzureOpenAIEmbeddingSkill uses the Search service system-assigned
managed identity to call Azure OpenAI (no apiKey in the skill body). Ensure the Search
service identity has the "Cognitive Services OpenAI User" role on the OpenAI resource.

For local dev set AZURE_OPENAI_KEY; the key is included in the skill body.
"""

import logging

import config as cfg
from utils.http import rest_put

logger = logging.getLogger(__name__)


def create_skillset() -> None:
    """Create or update the enrichment skillset via the REST API."""
    # Build the embedding skill; only include apiKey for local dev.
    embedding_skill: dict = {
        "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
        "name": "openai-embeddings",
        "description": "Generate 1536-d vector embeddings via Azure OpenAI ada-002",
        "context": "/document",
        "resourceUri": cfg.OPENAI_ENDPOINT.rstrip("/"),
        "deploymentId": cfg.EMBEDDING_DEPLOYMENT,
        "modelName": cfg.EMBEDDING_MODEL,
        "inputs": [{"name": "text", "source": "/document/merged_content"}],
        "outputs": [{"name": "embedding", "targetName": "content_vector"}],
    }
    if cfg.OPENAI_KEY:
        embedding_skill["apiKey"] = cfg.OPENAI_KEY

    body = {
        "name": cfg.SKILLSET_NAME,
        "description": "Multimodal RAG: Doc Intelligence + OCR + Merge + Split + OpenAI Embeddings",
        "skills": [
            # 1 - Document Intelligence Layout
            {
                "@odata.type": "#Microsoft.Skills.Util.DocumentIntelligenceLayoutSkill",
                "name": "doc-intelligence-layout",
                "description": "Extract text, tables, and layout structure from PDFs as Markdown",
                "context": "/document",
                "inputs": [{"name": "file_data", "source": "/document/file_data"}],
                "outputs": [{"name": "markdown_document", "targetName": "layout_text"}],
            },
            # 2 - OCR Skill
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
            # 3 - Merge Skill
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
            # 4 - Split Skill
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
            # 5 - Azure OpenAI Embedding Skill
            embedding_skill,
        ],
    }

    rest_put(f"skillsets/{cfg.SKILLSET_NAME}", body)
    logger.info("Skillset '%s' ready.", cfg.SKILLSET_NAME)