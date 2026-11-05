# 0001: Formula images become cached LaTeX in the chunk text

Status: accepted (05-11-2026)

## Context
- The Anlage zu § 13g in EnWG has its formula as an image, which was dropped silently.
- Every claim must trace to a source, so the formula has to be in the chunk.

## Decision
- Read the image with PaddleOCR-VL-1.6 as a separate step.
- Cache the result in `data/processed/statutes/formulas.json`, keyed by the image SHA-1.
- Strip `$$` and `\mathsf` / `\mathrm` from the output.
- Insert it inline as `$$...$$` where the image stood. No new `Chunk` field.
- PaddleOCR is a dev dependency, imported only when an image is read.

## Alternatives
- OCR inside `render()`: re-reads images every run.
- `formulas` field on `Chunk`: more provenance, but the cache already has it.
- Other models: all made at least one error on the test image.

## Consequences
- Re-runs are identical and do not need PaddleOCR.
- The formula is machine-read, not verified. I tested one image.
- Hand corrections in the cache are lost if `data/` is wiped.
- CI installs PaddlePaddle on every run.
