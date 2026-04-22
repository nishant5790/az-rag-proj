"""
scripts/search.py – Interactive search CLI for the PDF index.

Usage
─────
  # Single query
  python scripts/search.py --query "quarterly revenue breakdown"

  # Specify search mode
  python scripts/search.py --query "AI trends" --mode semantic

  # Filter by author
  python scripts/search.py --query "budget" --filter "author eq 'Finance Team'"

  # Show facets (authors, key phrases)
  python scripts/search.py --facets

  # Interactive REPL
  python scripts/search.py --interactive
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# allow `python scripts/search.py` to resolve src/
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import load_config
from search_query import PDFSearchClient, SearchMode

console = Console()


def print_results(results, query: str) -> None:
    if not results:
        console.print("[yellow]No results found.[/]")
        return

    console.print(f"\n[bold]Found {len(results)} result(s) for[/] [cyan]'{query}'[/]\n")

    for i, r in enumerate(results, 1):
        # Build header
        score_str = f"score={r.score:.4f}"
        if r.reranker_score is not None:
            score_str += f"  reranker={r.reranker_score:.4f}"

        title = r.title or r.blob_name or r.id
        header = f"[bold]{i}. {title}[/]  [dim]{score_str}[/]"

        # Build body
        body_parts = []

        if r.captions:
            body_parts.append("[bold]Caption:[/] " + r.captions[0])
        elif r.merged_content:
            snippet = r.merged_content[:300].replace("\n", " ")
            body_parts.append(f"[dim]{snippet}…[/]")

        if r.answers:
            body_parts.append("\n[bold]Answer:[/] " + r.answers[0])

        meta_parts = []
        if r.author:
            meta_parts.append(f"Author: {r.author}")
        if r.page_count:
            meta_parts.append(f"Pages: {r.page_count}")
        if r.key_phrases:
            meta_parts.append("Keywords: " + ", ".join(r.key_phrases[:5]))
        if r.blob_url:
            meta_parts.append(f"[link={r.blob_url}]{r.blob_url}[/link]")

        if meta_parts:
            body_parts.append("\n[dim]" + "  |  ".join(meta_parts) + "[/]")

        console.print(Panel("\n".join(body_parts), title=header, border_style="blue"))


def print_facets(client: PDFSearchClient) -> None:
    facet_data = client.facets(["author", "key_phrases"])

    for field, counts in facet_data.items():
        table = Table(title=f"Facet: {field}", show_header=True)
        table.add_column("Value", style="cyan")
        table.add_column("Count", justify="right")
        for item in counts[:20]:
            table.add_row(str(item["value"]), str(item["count"]))
        console.print(table)


def interactive_loop(client: PDFSearchClient, mode: SearchMode) -> None:
    console.print(
        Panel(
            f"[bold cyan]PDF Search REPL[/]  mode=[yellow]{mode}[/]\n"
            "Type a query and press Enter. Type [bold]quit[/] to exit.\n"
            "Commands: [bold]/mode <mode>[/]  [bold]/facets[/]  [bold]/count[/]",
            border_style="cyan",
        )
    )

    current_mode: SearchMode = mode

    while True:
        try:
            query = console.input("\n[bold green]>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            break

        if not query:
            continue
        if query.lower() in ("quit", "exit", "q"):
            break
        if query.startswith("/mode "):
            current_mode = query.split(None, 1)[1].strip()  # type: ignore[assignment]
            console.print(f"[yellow]Mode changed to: {current_mode}[/]")
            continue
        if query == "/facets":
            print_facets(client)
            continue
        if query == "/count":
            console.print(f"[bold]Documents in index:[/] {client.count()}")
            continue

        results = client.search(query, mode=current_mode, top=5)
        print_results(results, query)


def main() -> None:
    p = argparse.ArgumentParser(description="Search the Azure PDF index")
    p.add_argument("--query",       "-q", type=str, help="Search query")
    p.add_argument("--mode",        "-m", default="semantic",
                   choices=["full_text", "semantic", "vector", "hybrid"],
                   help="Search mode (default: semantic)")
    p.add_argument("--top",         "-n", type=int, default=5,
                   help="Number of results (default: 5)")
    p.add_argument("--filter",      type=str, default=None,
                   help="OData filter expression")
    p.add_argument("--facets",      action="store_true",
                   help="Show index facets and exit")
    p.add_argument("--count",       action="store_true",
                   help="Show total document count and exit")
    p.add_argument("--interactive", "-i", action="store_true",
                   help="Launch interactive REPL")
    args = p.parse_args()

    cfg = load_config()
    client = PDFSearchClient(cfg)

    if args.facets:
        print_facets(client)
    elif args.count:
        console.print(f"Documents in index: {client.count()}")
    elif args.interactive:
        interactive_loop(client, mode=args.mode)
    elif args.query:
        results = client.search(
            args.query,
            mode=args.mode,
            top=args.top,
            filter_expr=args.filter,
        )
        print_results(results, args.query)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
