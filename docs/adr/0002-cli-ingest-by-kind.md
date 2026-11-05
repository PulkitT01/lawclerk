# 0002: CLI with `ingest` by kind of document

Status: accepted (05-11-2026)

## Context
- Ingest only ran as `python -m lawclerk.ingest.statutes`.
- Rulings (BK6) will come later and must not run together with statutes by accident.

## Decision
- `uv run lawclerk ingest statutes` ingests all statutes.
- `uv run lawclerk ingest` with no kind ingests every kind (only statutes for now).
- Kinds are registered in `INGESTERS` in `src/lawclerk/cli.py`. Rulings will be one more line.
- argparse from the standard library, so no new dependency.
- The `lawclerk` script now points at `lawclerk.cli:main`.

## Alternatives
- `ingest enwg` (one law per command): the kind is what I need to separate, not the law.
- Typer or Click: nicer help, but a new runtime dependency for one subcommand.

## Consequences
- No way to ingest a single law from the CLI. It always runs all laws of a kind.
- A slow OCR run can start with plain `ingest`. Cached images make it fast after the first time.
- It refuses to run outside the repo root, because the `data/` paths are relative.
