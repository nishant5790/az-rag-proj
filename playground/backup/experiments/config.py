"""
config.py – Centralised configuration loaded from environment / .env file.
All other modules import from here; nothing reads os.environ directly.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root (two levels up from src/)
# _env_path = Path(__file__).parent.parent / ".env"
# load_dotenv(_env_path)
load_dotenv()

def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example → .env and fill in your values."
        )
    return value


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ─── Blob Storage ─────────────────────────────────────────────────────────────

@dataclass
class BlobConfig:
    connection_string: str = field(
        default_factory=lambda: _require("AZURE_STORAGE_CONNECTION_STRING")
    )
    account_name: str = field(
        default_factory=lambda: _require("AZURE_STORAGE_ACCOUNT_NAME")
    )
    account_key: str = field(
        default_factory=lambda: _require("AZURE_STORAGE_ACCOUNT_KEY")
    )
    container_name: str = field(
        default_factory=lambda: _optional("AZURE_BLOB_CONTAINER_NAME", "pdf-documents")
    )

    @property
    def container_url(self) -> str:
        return (
            f"https://{self.account_name}.blob.core.windows.net/{self.container_name}"
        )


# ─── Azure AI Search ──────────────────────────────────────────────────────────

@dataclass
class SearchConfig:
    endpoint: str = field(
        default_factory=lambda: _require("AZURE_SEARCH_SERVICE_ENDPOINT")
    )
    admin_key: str = field(
        default_factory=lambda: _require("AZURE_SEARCH_ADMIN_KEY")
    )
    index_name: str = field(
        default_factory=lambda: _optional("AZURE_SEARCH_INDEX_NAME", "pdf-search-index")
    )
    datasource_name: str = field(
        default_factory=lambda: _optional("AZURE_SEARCH_DATASOURCE_NAME", "pdf-blob-datasource")
    )
    skillset_name: str = field(
        default_factory=lambda: _optional("AZURE_SEARCH_SKILLSET_NAME", "pdf-enrichment-skillset")
    )
    indexer_name: str = field(
        default_factory=lambda: _optional("AZURE_SEARCH_INDEXER_NAME", "pdf-blob-indexer")
    )


# ─── Azure Document Intelligence ──────────────────────────────────────────────

@dataclass
class DocIntelligenceConfig:
    endpoint: str = field(
        default_factory=lambda: _require("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    )
    key: str = field(
        default_factory=lambda: _require("AZURE_DOCUMENT_INTELLIGENCE_KEY")
    )


# ─── Azure OpenAI (optional) ──────────────────────────────────────────────────

@dataclass
class OpenAIConfig:
    endpoint: str = field(default_factory=lambda: _optional("AZURE_OPENAI_ENDPOINT"))
    key: str = field(default_factory=lambda: _optional("AZURE_OPENAI_KEY"))
    embedding_deployment: str = field(
        default_factory=lambda: _optional(
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"
        )
    )
    dimensions: int = field(
        default_factory=lambda: int(_optional("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "3072"))
    )

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint and self.key)


# ─── Pipeline ─────────────────────────────────────────────────────────────────

@dataclass
class PipelineConfig:
    enable_vector_search: bool = field(
        default_factory=lambda: _optional("ENABLE_VECTOR_SEARCH", "false").lower() == "true"
    )
    chunk_size: int = field(
        default_factory=lambda: int(_optional("CHUNK_SIZE", "2000"))
    )
    chunk_overlap: int = field(
        default_factory=lambda: int(_optional("CHUNK_OVERLAP", "200"))
    )


# ─── Root Config ──────────────────────────────────────────────────────────────

@dataclass
class AppConfig:
    blob: BlobConfig = field(default_factory=BlobConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    doc_intelligence: DocIntelligenceConfig = field(default_factory=DocIntelligenceConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)


def load_config() -> AppConfig:
    """Instantiate and return a fully validated AppConfig."""
    return AppConfig()
