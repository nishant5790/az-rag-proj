"""
blob_sas_client.py – Azure Blob Storage operations using SAS Token + URL.

Supports:
  • LIST             – list all blobs (with metadata, size, last modified)
  • READ             – download / stream blob content
  • UPLOAD           – upload a new blob (file or raw bytes), with overwrite option
  • DELETE BLOB      – remove a blob
  • CREATE CONTAINER – create a new container (requires account-level SAS)
  • DELETE CONTAINER – delete a container and all its blobs (requires account-level SAS)

Authentication: SAS token only — no account key required.

────────────────────────────────────────────────────────────────
Configuration (pick ONE approach):

  A) Environment variables (recommended):
       AZURE_BLOB_SAS_URL   = https://<account>.blob.core.windows.net/<container>?<sas_token>
       # OR split form:
       AZURE_STORAGE_ACCOUNT_NAME = myaccount
       AZURE_BLOB_CONTAINER_NAME  = mycontainer
       AZURE_BLOB_SAS_TOKEN       = sv=2022-11-02&ss=b&srt=co&sp=rwdlacupiytfx&...

  B) Pass directly to BlobSASClient(container_sas_url="...", sas_token="...")

────────────────────────────────────────────────────────────────
CLI usage:

  python blob_sas_client.py list
  python blob_sas_client.py read   report.pdf --out ./downloads/report.pdf
  python blob_sas_client.py read   notes.txt  --text            # print to stdout
  python blob_sas_client.py add    ./invoice.pdf
  python blob_sas_client.py add    ./invoice.pdf --name docs/invoice-2024.pdf
  python blob_sas_client.py write  existing.txt --data "new content"
  python blob_sas_client.py write  existing.txt --file ./updated.txt
  python blob_sas_client.py delete old-report.pdf
"""

from __future__ import annotations
import argparse
import logging
import mimetypes
import os
import sys
from datetime import datetime , timezone
from pathlib import Path
from traceback import print_tb
from typing import Iterator
from dotenv import load_dotenv
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError,HttpResponseError
from azure.storage.blob import BlobServiceClient,BlobClient,ContainerClient, ContentSettings
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn,BarColumn,TextColumn,TransferSpeedColumn

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


# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    # filename="blob_sas_client.log",
    level=logging.WARNING,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
logger = logging.getLogger("blob_sas")
console = Console()

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a container SAS URL from parts
# ─────────────────────────────────────────────────────────────────────────────

def _build_container_sas_url(
        account_name: str,
        container_name: str,
        sas_token: str,
)-> str:
    """Assemble a full container SAS URL."""
    token = sas_token.lstrip("?")
    return f"https://{account_name}.blob.core.windows.net/{container_name}?{token}"

def _build_blob_sas_url(
        account_name: str,
        container_name: str,
        blob_name: str,
        sas_token: str,
) -> str:
    """Assemble a full blob SAS URL."""
    token = sas_token.lstrip("?")
    return f"https://{account_name}.blob.core.windows.net/{container_name}/{blob_name}?{token}"

# ─────────────────────────────────────────────────────────────────────────────
# Error handler
# ─────────────────────────────────────────────────────────────────────────────

def _handle_http_error(operation: str, exc: HttpResponseError) -> None:
    status = exc.status_code
    if status == 403:
        raise PermissionError(
            f"[{operation}] 403 Forbidden – SAS token is missing required permissions "
            f"or has expired. Needed permissions for this operation: "
            f"list=l  read=r  write=w  add=a  delete=d  create=c\n"
            f"Raw: {exc.message}"
        ) from exc
    if status == 404:
        raise FileNotFoundError(
            f"[{operation}] 404 Not Found – container or blob does not exist."
        ) from exc
    if status == 409:
        raise FileExistsError(
            f"[{operation}] 409 Conflict – resource already exists."
        ) from exc
    raise RuntimeError(f"[{operation}] HTTP {status}: {exc.message}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Core client
# ─────────────────────────────────────────────────────────────────────────────
class BlobSASClient:
    """
    Perform read /write/add/list / Delete on Azure Blob Storage.
    using only a SAS token
    Parameters
    ----------
    container_sas_url : Full SAS URL to the container, e.g.:
        https://myaccount.blob.core.windows.net/mycontainer?sv=...&sp=rwdl...
        If not provided, built from account_name + container_name + sas_token.
    account_name      : Storage account name (used if container_sas_url not set)
    container_name    : Container name       (used if container_sas_url not set)
    sas_token         : SAS token string     (used if container_sas_url not set)

    """
    def __init__(
            self,
            account_name: str|None = None,
            container_name: str|None = None,
            sas_token: str|None = None,
            container_sas_url: str|None = None,
    ) -> None:
        # ── Resolve the container SAS URL ─────────────────────────────────
        if container_sas_url:
            self.container_sas_url= container_sas_url
        else:
            _acct = account_name or os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
            _cont = container_name or os.getenv("AZURE_BLOB_CONTAINER_NAME")
            _tok  = sas_token or os.getenv("AZURE_BLOB_SAS_TOKEN")
            # Try the all-in-one env var as fallback
            _full = os.getenv("BLOB_SAS_URL")
            if _full:
                self._container_url = _full
            elif _acct and _cont and _tok:
                self._container_url = _build_container_sas_url(_acct, _cont, _tok)
            else:
                raise ValueError(
                    "Provide either 'container_sas_url' or the trio "
                    "(account_name, container_name, sas_token). "
                    "Alternatively set BLOB_SAS_URL in your .env file."
                )
        #Parse account + container from the url for display purposes
        from urllib.parse import urlparse
        parsed = urlparse(self._container_url)
        self._account_name = parsed.hostname.split(".")[0]
        path_parts = parsed.path.strip("/").split("/", 1)
        self._container_name = path_parts[0] if path_parts else ""
        self._sas_query = parsed.query
        self._container: ContainerClient= ContainerClient.from_container_url(
            self._container_url )

        logger.info(
            "SAS client initialised → account=%s  container=%s",
            self._account_name,
            self._container_name,
        )

    # ── Internal: get a BlobClient for a named blob ───────────────────────────

    def _blob_client(self,blob_name: str) -> BlobClient:
        """ Return a BlobClient for a blob name """
        blob_url = f"https://{self._account_name}.blob.core.windows.net/{self._container_name}/{blob_name}?{self._sas_query}"
        return  BlobClient.from_blob_url(blob_url)

    # ─────────────────────────────────────────────────────────────────────────
    # LIST
    # ─────────────────────────────────────────────────────────────────────────

    def list_blobs(self,
                   prefix:str="",
                   include_metadata:bool=True,
                   )-> list[dict]:
        """
        List all blobs in the container (optionally filtered by prefix).

        Returns
        -------
        List of dicts with: name, size, content_type, last_modified, etag,
        url, metadata.
        :param prefix:
        :param include_metadata:
        :return:
        """
        results = []
        try:
            for blob in self._container.list_blobs(
                name_starts_with=prefix or None,
                include= ["metadata"] if include_metadata else [],
            ):
                results.append(
                    {
                        "name": blob.name,
                        "size": blob.size,
                        "content_type": blob.content_settings.content_type if blob.content_settings.content_type else "",
                        "last_modified": blob.last_modified.isoformat(),
                        "etag": blob.etag,
                        "url":f"https://{self._account_name}.blob.core.windows.net/{self._container_name}/{blob.name}",
                        "metadata": dict(blob.metadata) if blob.metadata else {},
                    }
                )
        except HttpResponseError as exc:
            _handle_http_error("LIST",exc)

        return results


    def iter_blob_names(self,prefix:str="")->Iterator[str]:
        """Yield all blob names in the container (optionally filtered by prefix)."""
        for blob in self._container.list_blobs(name_starts_with=prefix or None):
            yield blob.name


    # ─────────────────────────────────────────────────────────────────────────
    # READ
    # ─────────────────────────────────────────────────────────────────────────

    def read_bytes(self,blob_name:str)->bytes:
        """ Download a blob and return its raw bytes"""
        try:
            client = self._blob_client(blob_name)
            data= client.download_blob().readall()
            logger.info("READ  '%s'  (%d bytes)", blob_name, len(data))
            return data
        except ResourceNotFoundError:
            raise FileNotFoundError( f"Blob '{blob_name}' not found")
        except HttpResponseError as exc:
            _handle_http_error("READ",exc)

    def download_to_file(self,blob_name:str , dest_path:str|Path,show_progress:bool=True)->Path:
        """
        Download a blob to a local file.

        Parameters
        ----------
        blob_name  : Name of the blob in the container.
        dest_path  : Local destination path. If it's a directory, the blob
                     filename is appended automatically.
        show_progress : Display a progress bar (default True).

        Returns
        -------
        The resolved local path of the downloaded file.

        """
        dest = Path(dest_path)
        if dest.is_dir():
            dest = dest/Path(blob_name).name
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            client = self._blob_client(blob_name)
            props = client.get_blob_properties()
            total = props.size

            if show_progress:
                with Progress(SpinnerColumn(),TextColumn("[cyan]{task.description}"),BarColumn(),TransferSpeedColumn(),console=console) as progress:
                    task = progress.add_task(f"Downloading {blob_name}", total=total)
                    stream = client.download_blob()
                    with open(dest, "wb") as f:
                        for chunk in stream.chunks():
                            f.write(chunk)
                            progress.advance(task,advance=len(chunk))
            else:
                stream = client.download_blob()
                with open(dest, "wb") as f:
                    stream.readinto(f)

            logger.info("DOWNLOAD  '%s'  →  %s", blob_name, dest)
            return dest
        except ResourceNotFoundError:
            raise FileNotFoundError( f"Blob '{blob_name}' not found")
        except HttpResponseError as exc:
            _handle_http_error("READ",exc)

    # ─────────────────────────────────────────────────────────────────────────
    # CREATE CONTAINER
    # ─────────────────────────────────────────────────────────────────────────

    def create_container(
            self,
            container_name: str,
            metadata: dict | None = None,
            exist_ok: bool = False,
    ) -> str:
        """
        Create a new container in the storage account.

        Requires a service-level (account) SAS with resource type 'c' and
        permission 'c' (create).

        Parameters
        ----------
        container_name : Name of the container to create.
        metadata       : Optional key-value metadata to attach to the container.
        exist_ok       : If True, silently succeed when the container already
                         exists. If False (default), raise FileExistsError.

        Returns
        -------
        URL of the newly created (or existing) container.
        """
        service_url = f"https://{self._account_name}.blob.core.windows.net?{self._sas_query}"
        service_client = BlobServiceClient(account_url=service_url)
        try:
            container_client = service_client.create_container(
                container_name,
                metadata=metadata,
            )
            url = f"https://{self._account_name}.blob.core.windows.net/{container_name}"
            logger.info("CREATE CONTAINER  '%s'", container_name)
            return url
        except ResourceExistsError:
            if exist_ok:
                return f"https://{self._account_name}.blob.core.windows.net/{container_name}"
            raise FileExistsError(
                f"Container '{container_name}' already exists."
            )
        except HttpResponseError as exc:
            _handle_http_error("CREATE CONTAINER", exc)

    # ─────────────────────────────────────────────────────────────────────────
    # UPLOAD
    # ─────────────────────────────────────────────────────────────────────────

    def upload_blob(
            self,
            blob_name: str,
            data: bytes | str | Path,
            overwrite: bool = False,
            content_type: str | None = None,
            metadata: dict | None = None,
            show_progress: bool = True,
    ) -> str:
        """
        Upload data to a blob.

        Parameters
        ----------
        blob_name    : Destination blob name (path) within the container.
        data         : Bytes to upload, a string (encoded as UTF-8), or a
                       Path/str pointing to a local file.
        overwrite    : Overwrite the blob if it already exists (default False).
        content_type : MIME type. Auto-detected from blob_name if not given.
        metadata     : Optional key-value metadata dict.
        show_progress: Display a progress bar for file uploads (default True).

        Returns
        -------
        The blob URL (without SAS query).

        Raises
        ------
        FileExistsError  : blob already exists and overwrite=False.
        FileNotFoundError: local file path does not exist.
        """
        # ── Resolve bytes payload ─────────────────────────────────────────
        if isinstance(data, (str, Path)):
            local = Path(data)
            if not local.exists():
                raise FileNotFoundError(f"Local file not found: {local}")
            raw: bytes = local.read_bytes()
            if content_type is None:
                content_type, _ = mimetypes.guess_type(str(local))
        elif isinstance(data, bytes):
            raw = data
        else:
            raise TypeError("'data' must be bytes, str, or Path.")

        if content_type is None:
            content_type, _ = mimetypes.guess_type(blob_name)
        content_settings = ContentSettings(content_type=content_type) if content_type else None

        client = self._blob_client(blob_name)
        try:
            if show_progress:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[cyan]{task.description}"),
                    BarColumn(),
                    TransferSpeedColumn(),
                    console=console,
                ) as progress:
                    progress.add_task(f"Uploading {blob_name}", total=len(raw))
                    client.upload_blob(
                        raw,
                        overwrite=overwrite,
                        content_settings=content_settings,
                        metadata=metadata,
                    )
            else:
                client.upload_blob(
                    raw,
                    overwrite=overwrite,
                    content_settings=content_settings,
                    metadata=metadata,
                )
        except ResourceExistsError:
            raise FileExistsError(
                f"Blob '{blob_name}' already exists. Use overwrite=True to replace it."
            )
        except HttpResponseError as exc:
            _handle_http_error("UPLOAD", exc)

        url = f"https://{self._account_name}.blob.core.windows.net/{self._container_name}/{blob_name}"
        logger.info("UPLOAD  '%s'  (%d bytes)", blob_name, len(raw))
        return url

    # ─────────────────────────────────────────────────────────────────────────
    # DELETE BLOB
    # ─────────────────────────────────────────────────────────────────────────

    def delete_blob(
            self,
            blob_name: str,
            not_found_ok: bool = False,
            delete_snapshots: bool = True,
    ) -> None:
        """
        Delete a blob from the container.

        Parameters
        ----------
        blob_name       : Name of the blob to delete.
        not_found_ok    : If True, silently succeed when the blob does not
                          exist. If False (default), raise FileNotFoundError.
        delete_snapshots: Also delete any snapshots of the blob (default True).
        """
        client = self._blob_client(blob_name)
        try:
            client.delete_blob(
                delete_snapshots="include" if delete_snapshots else "only",
            )
            logger.info("DELETE  '%s'", blob_name)
        except ResourceNotFoundError:
            if not_found_ok:
                return
            raise FileNotFoundError(f"Blob '{blob_name}' not found.")
        except HttpResponseError as exc:
            _handle_http_error("DELETE", exc)

    # ─────────────────────────────────────────────────────────────────────────
    # DELETE CONTAINER
    # ─────────────────────────────────────────────────────────────────────────

    def delete_container(
            self,
            container_name: str,
            not_found_ok: bool = False,
    ) -> None:
        """
        Delete a container and all blobs inside it.

        Requires a service-level (account) SAS with resource type 'c' and
        permission 'd' (delete).

        Parameters
        ----------
        container_name : Name of the container to delete.
        not_found_ok   : If True, silently succeed when the container does not
                         exist. If False (default), raise FileNotFoundError.

        Warning
        -------
        This is irreversible. All blobs within the container are permanently
        deleted.
        """
        service_url = f"https://{self._account_name}.blob.core.windows.net?{self._sas_query}"
        service_client = BlobServiceClient(account_url=service_url)
        try:
            service_client.delete_container(container_name)
            logger.info("DELETE CONTAINER  '%s'", container_name)
        except ResourceNotFoundError:
            if not_found_ok:
                return
            raise FileNotFoundError(f"Container '{container_name}' not found.")
        except HttpResponseError as exc:
            _handle_http_error("DELETE CONTAINER", exc)


if __name__ == "__main__":
    import json
    # ── Build client ──────────────────────────────────────────────────────

    client = BlobSASClient()

    # print(f"printing the list of blob names....\n")
    # for blob in client.iter_blob_names():
    #     print(blob)
    #
    # print(f"printing the list of blob names using list blobs....\n")
    # output = client.list_blobs()
    # for data in output:
    #     print(json.dumps(data, indent=2))
    #     print("\n")

    print(f"creating a new container named 'test-container'....\n")
    client.create_container("test-container",exist_ok=True)

    print(f"Deleting the container named 'test-container'....\n")
    client.delete_container("test-container", not_found_ok=True)

    # print(f"reading the blob")
    # data = client.read_bytes(blob_name="PN-75998_1.pdf")


    # print(f"downloading the blob")
    # client.download_to_file(blob_name="PN-75998_1.pdf",dest_path="data/")






