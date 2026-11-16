"""Fetch the BK6 documents of the Bundesnetzagentur into data/raw/rulings/, one snapshot per run.

Each run crawls the two BK6 lists and every page they lead to inside the boundary, downloads
only what is new or changed, and writes runs/<run>.jsonl. Nothing stored is overwritten or
deleted: a changed file is stored beside the old one. See ADR 0004.

Run from the repository root: ``uv run lawclerk ingest rulings``
"""

from lawclerk.ingest.rulings.run import main

__all__ = ["main"]
