"""
main.py – CLI entry point for the Azure Multimodal RAG pipeline.

Run from the azure-mmr/ directory (or from the project root via
`python azure-mmr/main.py`).

Commands:
    setup              Provision all resources and run the indexer
    query <text>       Search the index with a natural language query
    delete             Tear down all provisioned resources
    status             Check the current indexer run status
"""

import sys
import logging

logger = logging.getLogger(__name__)

_HELP = """\
Azure Multimodal RAG Pipeline
==============================
Usage:
  python main.py setup              Provision data source, index, skillset,
                                    indexer — then wait for indexing to finish.
  python main.py query <text>       Full-text search the index.
  python main.py delete             Delete all provisioned resources.
  python main.py status             Show current indexer run status.
"""


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "setup":
        from pipeline import setup
        setup()

    elif cmd == "query":
        query_text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "summary"
        from search import MMRSearch
        MMRSearch().print_results(query_text)

    elif cmd == "delete":
        from pipeline import teardown
        teardown()

    elif cmd == "status":
        from setup import wait_for_indexer
        result = wait_for_indexer(poll_interval=5, timeout=30)
        print(
            f"\nStatus    : {result.get('status')}\n"
            f"Processed : {result.get('itemsProcessed', 0)}\n"
            f"Failed    : {result.get('itemsFailed', 0)}"
        )

    else:
        print(_HELP)


if __name__ == "__main__":
    main()
