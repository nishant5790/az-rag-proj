"""
blob_manager.py – All Azure Blob Storage operations.

Responsibilities
────────────────
• Create / ensure container exists
• Upload single PDF or a whole directory
• List blobs (with optional prefix filter)
• Delete blobs
• Generate time-limited SAS URLs (used by the indexer)
• Stream-download a blob back to local disk
"""

from __future__ import annotations

import logging
import mimetypes
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import (
    BlobServiceClient,
    BlobClient,
    ContainerClient,
    ContentSettings,
    generate_blob_sas,
    BlobSasPermissions,
)
from tqdm import tqdm

from config import BlobConfig

logger = logging.getLogger(__name__)


class BlobManager:
    """High-level wrapper around Azure Blob Storage for PDF management."""

    def __init__(self, cfg: BlobConfig) -> None:
        self.cfg = cfg
        self._service: BlobServiceClient = BlobServiceClient.from_connection_string(
            cfg.connection_string
        )
        self._container: ContainerClient = self._service.get_container_client(
            cfg.container_name
        )

    # ── Container ─────────────────────────────────────────────────────────────

    def ensure_container(self, public_access: str | None = None) -> None:
        """Create the container if it does not exist."""
        try:
            self._container.create_container(public_access=public_access)
            logger.info("Container '%s' created.", self.cfg.container_name)
        except ResourceExistsError:
            logger.debug("Container '%s' already exists.", self.cfg.container_name)

    def delete_container(self) -> None:
        """Permanently delete the container and all blobs inside."""
        try:
            self._container.delete_container()
            logger.warning("Container '%s' deleted.", self.cfg.container_name)
        except ResourceNotFoundError:
            logger.debug("Container '%s' did not exist.", self.cfg.container_name)

    # ── Upload ─────────────────────────────────────────────────────────────────

    def upload_pdf(
        self,
        local_path: str | Path,
        blob_name: str | None = None,
        overwrite: bool = True,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """
        Upload a single PDF to the container.

        Parameters
        ----------
        local_path : path to the local file
        blob_name  : destination name inside the container; defaults to filename
        overwrite  : whether to overwrite an existing blob
        metadata   : optional key/value pairs stored with the blob

        Returns
        -------
        The full blob URL.
        """
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"Expected a .pdf file, got: {path.suffix}")

        blob_name = blob_name or path.name
        blob_client: BlobClient = self._container.get_blob_client(blob_name)

        mime_type = mimetypes.guess_type(str(path))[0] or "application/pdf"
        content_settings = ContentSettings(content_type=mime_type)

        with open(path, "rb") as fh:
            blob_client.upload_blob(
                fh,
                overwrite=overwrite,
                content_settings=content_settings,
                metadata=metadata or {},
            )

        url = blob_client.url
        logger.info("Uploaded '%s' → %s", path.name, url)
        return url

    def upload_directory(
        self,
        directory: str | Path,
        prefix: str = "",
        overwrite: bool = True,
    ) -> list[str]:
        """
        Recursively upload all PDF files from a local directory.

        Parameters
        ----------
        directory : root folder to scan
        prefix    : optional virtual folder prefix in the container
        overwrite : whether to overwrite existing blobs

        Returns
        -------
        List of uploaded blob URLs.
        """
        root = Path(directory)
        if not root.is_dir():
            raise NotADirectoryError(f"Not a directory: {root}")

        pdf_files = sorted(root.rglob("*.pdf"))
        if not pdf_files:
            logger.warning("No PDF files found in '%s'.", root)
            return []

        urls: list[str] = []
        for pdf in tqdm(pdf_files, desc="Uploading PDFs", unit="file"):
            relative = pdf.relative_to(root)
            blob_name = f"{prefix}/{relative}".lstrip("/") if prefix else str(relative)
            # Normalise path separators for blob names
            blob_name = blob_name.replace("\\", "/")
            url = self.upload_pdf(pdf, blob_name=blob_name, overwrite=overwrite)
            urls.append(url)

        logger.info("Uploaded %d PDF(s) from '%s'.", len(urls), root)
        return urls

    # ── List ──────────────────────────────────────────────────────────────────

    def list_blobs(self, prefix: str = "") -> list[dict]:
        """
        Return metadata for all blobs (optionally filtered by prefix).

        Returns list of dicts with keys: name, size, last_modified, url.
        """
        results = []
        for blob in self._container.list_blobs(name_starts_with=prefix or None):
            results.append(
                {
                    "name": blob.name,
                    "size": blob.size,
                    "last_modified": blob.last_modified,
                    "url": f"https://{self.cfg.account_name}.blob.core.windows.net"
                           f"/{self.cfg.container_name}/{blob.name}",
                }
            )
        return results

    def iter_blob_names(self, prefix: str = "") -> Iterator[str]:
        """Yield blob names lazily."""
        for blob in self._container.list_blobs(name_starts_with=prefix or None):
            yield blob.name

    # ── Delete ─────────────────────────────────────────────────────────────────

    def delete_blob(self, blob_name: str) -> None:
        """Delete a single blob."""
        try:
            self._container.get_blob_client(blob_name).delete_blob()
            logger.info("Deleted blob '%s'.", blob_name)
        except ResourceNotFoundError:
            logger.warning("Blob '%s' not found; skipping delete.", blob_name)

    def delete_all_blobs(self, prefix: str = "") -> int:
        """Delete all blobs matching an optional prefix. Returns count deleted."""
        names = list(self.iter_blob_names(prefix))
        for name in tqdm(names, desc="Deleting blobs", unit="blob"):
            self.delete_blob(name)
        return len(names)

    # ── Download ──────────────────────────────────────────────────────────────

    def download_blob(self, blob_name: str, dest_path: str | Path) -> Path:
        """Download a blob to a local file. Returns the destination path."""
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob_client = self._container.get_blob_client(blob_name)
        with open(dest, "wb") as fh:
            stream = blob_client.download_blob()
            stream.readinto(fh)
        logger.info("Downloaded '%s' → %s", blob_name, dest)
        return dest

    # ── SAS URL ───────────────────────────────────────────────────────────────

    def generate_sas_url(
        self,
        blob_name: str,
        expiry_hours: int = 24,
        permissions: BlobSasPermissions | None = None,
    ) -> str:
        """
        Generate a time-limited SAS URL for a blob.

        The URL grants read access by default, which is the minimum needed for
        the Azure AI Search indexer to crawl the container.
        """
        perms = permissions or BlobSasPermissions(read=True)
        expiry = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)

        sas_token = generate_blob_sas(
            account_name=self.cfg.account_name,
            container_name=self.cfg.container_name,
            blob_name=blob_name,
            account_key=self.cfg.account_key,
            permission=perms,
            expiry=expiry,
        )
        return (
            f"https://{self.cfg.account_name}.blob.core.windows.net"
            f"/{self.cfg.container_name}/{blob_name}?{sas_token}"
        )

    # ── Container SAS (for the Search indexer) ────────────────────────────────

    def generate_container_sas(self, expiry_hours: int = 8760) -> str:
        """
        Generate a SAS token scoped to the entire container (read + list).
        Used when creating the Search data source connection string.
        Default expiry: 1 year.
        """
        from azure.storage.blob import generate_container_sas, ContainerSasPermissions

        expiry = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
        sas_token = generate_container_sas(
            account_name=self.cfg.account_name,
            container_name=self.cfg.container_name,
            account_key=self.cfg.account_key,
            permission=ContainerSasPermissions(read=True, list=True),
            expiry=expiry,
        )
        return sas_token
