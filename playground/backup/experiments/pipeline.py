"""
pipeline.py – End-to-end orchestration of the Azure PDF search pipeline.

This is the single entry-point you call to go from "local PDF files" to
"searchable Azure AI Search index" in one command.

Flow
────
  1. Validate config
  2. Create / ensure Blob Storage container
  3. Upload PDFs from a local directory (or a single file)
  4. Provision Azure AI Search resources:
       data source → index → skillset → indexer
  5. Trigger the indexer and (optionally) wait for completion
  6. Print a summary

CLI
───
  python pipeline.py --pdf-dir ./pdfs
  python pipeline.py --pdf-file ./report.pdf --wait
  python pipeline.py --teardown   # remove all Azure resources
  python pipeline.py --status     # check indexer status
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

from config import load_config
from blob_manager import BlobManager
from search_setup import SearchPipelineSetup
from search_query import PDFSearchClient

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
logger = logging.getLogger("pipeline")
console = Console()


# ── Pipeline class ────────────────────────────────────────────────────────────

class AzurePDFSearchPipeline:
    """
    Orchestrates the full blob-upload → search-index pipeline.
    """

    def __init__(self) -> None:
        console.print("[bold cyan]Loading configuration…[/]")
        self.cfg = load_config()
        self.blob = BlobManager(self.cfg.blob)
        self.search = SearchPipelineSetup(self.cfg)

    # ── Setup ─────────────────────────────────────────────────────────────────

    def setup_infrastructure(self) -> None:
        """
        Provision all Azure resources (idempotent – safe to call repeatedly).
        """
        console.rule("[bold]Step 1 – Blob Storage[/]")
        self.blob.ensure_container()
        console.print(f"[green]✓[/] Container [cyan]{self.cfg.blob.container_name}[/] ready.")

        console.rule("[bold]Step 2 – Azure AI Search[/]")
        console.print("Creating data source…")
        self.search.create_data_source(self.cfg.blob.connection_string)
        console.print("[green]✓[/] Data source ready.")

        console.print("Creating index schema…")
        self.search.create_index()
        console.print("[green]✓[/] Index ready.")

        console.print("Creating skillset (Doc Intelligence + OCR + NLP + chunking)…")
        self.search.create_skillset()
        console.print("[green]✓[/] Skillset ready.")

        console.print("Creating indexer…")
        self.search.create_indexer()
        console.print("[green]✓[/] Indexer ready.")

    # ── Upload ─────────────────────────────────────────────────────────────────

    def upload_pdfs(
        self,
        pdf_dir: str | Path | None = None,
        pdf_file: str | Path | None = None,
        blob_prefix: str = "",
    ) -> list[str]:
        """Upload PDFs from a directory or a single file. Returns blob URLs."""
        console.rule("[bold]Step 3 – Uploading PDFs[/]")

        if pdf_dir:
            urls = self.blob.upload_directory(pdf_dir, prefix=blob_prefix)
        elif pdf_file:
            url = self.blob.upload_pdf(pdf_file)
            urls = [url]
        else:
            console.print("[yellow]No PDF source specified – skipping upload.[/]")
            return []

        console.print(f"[green]✓[/] Uploaded [bold]{len(urls)}[/] PDF(s).")
        return urls

    # ── Index ─────────────────────────────────────────────────────────────────

    def run_indexer(self, wait: bool = True) -> dict | None:
        """Trigger the indexer and optionally wait for completion."""
        console.rule("[bold]Step 4 – Running Indexer[/]")
        self.search.run_indexer()
        console.print("[green]✓[/] Indexer triggered.")

        if wait:
            console.print("Waiting for indexer to complete…  (this may take a few minutes)")
            status = self.search.wait_for_indexer()
            self._print_indexer_status(status)
            return status
        else:
            console.print(
                "[yellow]Not waiting for indexer. "
                "Run with --wait to block until indexing finishes.[/]"
            )
            return None

    def _print_indexer_status(self, status: dict) -> None:
        run_status = status.get("status", "unknown")
        items_processed = status.get("itemsProcessed", 0)
        items_failed = status.get("itemsFailed", 0)
        start = status.get("startTime", "")
        end = status.get("endTime", "")
        errors = status.get("errors") or []

        color = "green" if run_status == "success" else "red"
        console.print(
            Panel(
                f"[bold {color}]Status: {run_status.upper()}[/]\n"
                f"Items processed: {items_processed}  |  Failed: {items_failed}\n"
                f"Start: {start}  →  End: {end}",
                title="[bold]Indexer Result[/]",
            )
        )

        if errors:
            console.print("[red]Errors:[/]")
            for err in errors[:5]:
                console.print(f"  • {err.get('key', '')} – {err.get('errorMessage', '')}")

    # ── Full pipeline ─────────────────────────────────────────────────────────

    def run(
        self,
        pdf_dir: str | Path | None = None,
        pdf_file: str | Path | None = None,
        blob_prefix: str = "",
        wait: bool = True,
    ) -> None:
        """
        Run the entire pipeline end-to-end:
          1. Provision Azure resources
          2. Upload PDFs
          3. Run indexer
        """
        console.print(
            Panel(
                "[bold cyan]Azure PDF Search Pipeline[/]\n"
                f"  Blob container : [white]{self.cfg.blob.container_name}[/]\n"
                f"  Search index   : [white]{self.cfg.search.index_name}[/]\n"
                f"  Vector search  : [white]{self.cfg.pipeline.enable_vector_search}[/]",
                title="[bold]Configuration[/]",
            )
        )

        self.setup_infrastructure()
        self.upload_pdfs(pdf_dir=pdf_dir, pdf_file=pdf_file, blob_prefix=blob_prefix)
        self.run_indexer(wait=wait)

        console.rule("[bold green]Pipeline Complete[/]")
        self._print_summary()

    # ── Status ────────────────────────────────────────────────────────────────

    def print_status(self) -> None:
        """Print current indexer status and index stats."""
        status = self.search.get_indexer_status()
        last = status.get("lastResult") or {}
        self._print_indexer_status(last)

        # Print blob count
        blobs = self.blob.list_blobs()
        console.print(f"\n[bold]Blobs in container:[/] {len(blobs)}")

        # Print index doc count
        try:
            client = PDFSearchClient(self.cfg)
            count = client.count()
            console.print(f"[bold]Documents in index:[/] {count}")
        except Exception:
            pass

    # ── Teardown ──────────────────────────────────────────────────────────────

    def teardown(self, delete_blobs: bool = False) -> None:
        """Delete all Azure AI Search resources (and optionally blobs)."""
        console.print("[bold red]Tearing down Azure resources…[/]")
        self.search.teardown()
        console.print("[green]✓[/] Search resources deleted.")

        if delete_blobs:
            count = self.blob.delete_all_blobs()
            console.print(f"[green]✓[/] Deleted {count} blob(s).")

    # ── Summary ───────────────────────────────────────────────────────────────

    def _print_summary(self) -> None:
        table = Table(title="Pipeline Summary", show_header=True)
        table.add_column("Resource", style="cyan")
        table.add_column("Name", style="white")

        table.add_row("Storage account",  self.cfg.blob.account_name)
        table.add_row("Blob container",   self.cfg.blob.container_name)
        table.add_row("Search endpoint",  self.cfg.search.endpoint)
        table.add_row("Search index",     self.cfg.search.index_name)
        table.add_row("Skillset",         self.cfg.search.skillset_name)
        table.add_row("Indexer",          self.cfg.search.indexer_name)
        table.add_row("Vector search",    str(self.cfg.pipeline.enable_vector_search))

        console.print(table)
        console.print(
            "\n[bold green]Your PDFs are now searchable![/]\n"
            "Run a search with:\n"
            "  [cyan]python scripts/search.py --query 'your query here'[/]\n"
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Azure PDF Search Pipeline – upload PDFs and index with Azure AI Search"
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--pdf-dir",  type=Path, help="Directory containing PDF files to upload")
    group.add_argument("--pdf-file", type=Path, help="Single PDF file to upload")

    p.add_argument("--prefix",   default="",    help="Blob name prefix (virtual folder)")
    p.add_argument("--wait",     action="store_true", default=True,
                   help="Wait for indexer to finish (default: True)")
    p.add_argument("--no-wait",  dest="wait", action="store_false",
                   help="Return immediately after triggering the indexer")
    p.add_argument("--setup-only", action="store_true",
                   help="Provision resources only, skip upload and indexer")
    p.add_argument("--upload-only", action="store_true",
                   help="Upload PDFs only, do not provision resources or run indexer")
    p.add_argument("--index-only", action="store_true",
                   help="Run the indexer only (resources + blobs already exist)")
    p.add_argument("--status",   action="store_true", help="Show indexer and index status")
    p.add_argument("--teardown", action="store_true",
                   help="Delete all search resources")
    p.add_argument("--delete-blobs", action="store_true",
                   help="Also delete blobs when tearing down")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    try:
        pipeline = AzurePDFSearchPipeline()
    except EnvironmentError as exc:
        console.print(f"[bold red]Configuration error:[/] {exc}")
        sys.exit(1)

    if args.status:
        pipeline.print_status()
    elif args.teardown:
        pipeline.teardown(delete_blobs=args.delete_blobs)
    elif args.setup_only:
        pipeline.setup_infrastructure()
    elif args.upload_only:
        pipeline.upload_pdfs(pdf_dir=args.pdf_dir, pdf_file=args.pdf_file, blob_prefix=args.prefix)
    elif args.index_only:
        pipeline.run_indexer(wait=args.wait)
    else:
        pipeline.run(
            pdf_dir=args.pdf_dir,
            pdf_file=args.pdf_file,
            blob_prefix=args.prefix,
            wait=args.wait,
        )


if __name__ == "__main__":
    main()
