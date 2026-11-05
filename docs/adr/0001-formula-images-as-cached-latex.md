# 0001: Formula images become cached LaTeX in the chunk text

Status: accepted (05-11-2026)

## Context
- The Anlage zu § 13g in EnWG has its formula as an image, which was dropped silently.
- Every claim must trace to a source, so the formula has to be in the chunk.

## Decision
- Read the image with PaddleOCR-VL-1.6 as a separate step.
- Cache the result in `data/processed/statutes/formulas.json`, keyed by the image SHA-1.
- Strip `\mathsf` / `\mathrm` from the output, so the formula matches the `V_{it}` in the definitions. Keep the model's `$$` delimiters.
- Insert it inline, as cached, where the image stood. No new `Chunk` field.
- Treat every `<IMG>` as a formula. Only one image exists so far.
- PaddleOCR is in the optional `ocr` extra (`uv sync --extra ocr`), imported only when an image is read.

## Alternatives
- OCR inside `render()`: re-reads images every run.
- `formulas` field on `Chunk`: more provenance, but the cache already has it.
- Other models: all made at least one error on the test image.

## Consequences
- Re-runs are identical and do not need PaddleOCR.
- The formula is machine-read, not verified. I tested one image.
- Hand corrections in the cache are lost if `data/` is wiped.
- When I add a law, I check its new entries in `formulas.json`. A diagram or table image would come out as made-up LaTeX.
- If the model ever returns no delimiters, the formula runs into the text.
- `uv sync` without `--extra ocr` removes PaddleOCR. CI doesn't install it.
