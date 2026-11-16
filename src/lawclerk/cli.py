"""Command line entry point: ``uv run lawclerk ingest [statutes|rulings]``."""

import argparse
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from lawclerk.ingest import rulings, statutes

# Kind of document -> function that ingests all documents of that kind
INGESTERS: Final[dict[str, Callable[[], None]]] = {
    "statutes": statutes.main,
    "rulings": rulings.main,
}


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the lawclerk command."""
    parser = argparse.ArgumentParser(prog="lawclerk")
    subcommands = parser.add_subparsers(dest="command", required=True)
    ingest = subcommands.add_parser("ingest", help="download and process the legal documents")
    ingest.add_argument(
        "kind",
        nargs="?",
        choices=list(INGESTERS),
        help="kind of document to ingest; all kinds if left out",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run the lawclerk command; argv defaults to the command line arguments."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # The data paths are relative, so running elsewhere would start a new, empty data/ folder
    if not Path("pyproject.toml").is_file():
        parser.error("run this from the repository root (the folder with pyproject.toml)")
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request otherwise
    kinds = [args.kind] if args.kind else list(INGESTERS)
    for kind in kinds:
        INGESTERS[kind]()
