# LawClerk

LawClerk is a multi-agent deep research system for German energy law (EnWG and
BNetzA BK6 rulings). Every claim in its output must trace back to a verifiable
retrieved source. It has two parts: **law-rag**, a hybrid-search retrieval service
over the legal texts, and a **supervisor/researcher agent** that queries law-rag
and web search, then writes a cited report.

It's a from-scratch reimplementation of EnergyXpert, my master's thesis system
at Fraunhofer, done with my former supervisor's approval. No original code,
prompts or data are reused.

## Commands

- Install: `uv sync` (add `--extra ocr` for formula OCR)
- Format: `uv run ruff format .`
- Lint: `uv run ruff check .`
- Type check: `uv run mypy src tests`
- Test: `uv run pytest`
- All hooks: `uv run pre-commit run --all-files`

All four checks must pass before a change is done. CI runs them on every PR.

## Layout

- `src/lawclerk/`: the package (src layout; import as `lawclerk`, never `src.lawclerk`)
- `tests/`: mirrors `src/`; `tests/unit/` has no network or database access
- `docs/adr/`: one file per design decision; `docs/devlog/`: daily notes
- `data/`: downloads and processed files, gitignored

Built: `ingest` (statutes), `cli`. Planned: retrieval, api, agent, evals.

## Conventions

- Python 3.13, dependencies managed with uv only (never pip)
- mypy strict: type hints on every function, including tests
- Every new function gets a test; tests must not call real APIs
- Commit messages use Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`, `test:`)
- Every change goes through a branch and a PR into `main`

## Working with me

My goal is to learn software engineering best practices while building this,
so I make the design decisions.

- **Ask before deciding** anything about structure, data models, interfaces,
  libraries, error handling or algorithms. Offer 2–3 options with trade-offs.
- **Just do** routine work: boilerplate, formatting, obvious fixes, test scaffolding.
- **Explain** why you chose an approach, briefly.
- **Never, without asking:** commit, push, add dependencies, or change tests
  just to make them pass.
