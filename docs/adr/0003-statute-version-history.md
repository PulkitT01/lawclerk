# 0003: Statute version history from the QuantLaw archive

Status: accepted (15-11-2026)

## Context
- Ingest kept only the latest EnWG text. Retrieval will need "as of date X".
- gesetze-im-internet.de serves only the current text.
- QuantLaw's `gesetze-im-internet` repo (branch `data`) has a daily scrape since 2019-06-10. 153 commits change the EnWG XML.

## Decision
- A version is an unbroken run of snapshots with identical title and text for one section. Key: law + section + `first_seen`.
- `doknr` and the Stand note never decide versions.
- A section that is missing (or empty) ends its version. When it returns, it starts a new one, even with the same text (§ 4e).
- Text that returns after a change (A, B, A) is a new version.
- Versions go in `<slug>.versions.jsonl` (`SectionVersion`). Chunks of every version go in `<slug>.jsonl`.
- Chunk id is `<old id>@<first_seen>`, with `version_id` linking to its version.
- `Chunk.version` is renamed to `stand_note`. A version keeps the note of its first-seen snapshot.
- Dates:
  - `first_seen` and `commit` are observed.
  - `valid_from` = `first_seen`, or `null` for versions already in the first snapshot (start unknown).
  - `valid_to` is exclusive: the first snapshot with a change or without the section.
  - No `last_seen`: the snapshots are only the commits that changed the file.
- As-of lookup (`version_at`): `valid_from` (or `first_seen`) <= day < `valid_to`. A day before 2019-06-10 finds nothing.
- `change` flag is a heuristic:
  - `doknr` same as the snapshot just before: `correction`.
  - New `doknr`: `amendment`.
  - Nothing just before: `initial`.
- Broken snapshots: skipped if they have fewer than 90% of the previous accepted snapshot's sections. No look-ahead, so it works on the newest snapshot.
- Storage: a bare blobless clone at `data/raw/statutes/archive.git`. `git fetch` adds new commits each run. Each snapshot is exported to a temp folder and parsed there.
- Fetching via `git` in a subprocess. No new dependency.
- Formula images come from the same commit as the XML and go through the existing OCR cache.
- `source_url`: the gesetze-im-internet.de page for open versions, the archive file at its commit for versions that ended.
- History comes only from the archive. The live download is a check:
  - Warn if its section texts differ from the newest snapshot.
  - Warn if the archive's newest scrape is over 3 days old.
- `lawclerk ingest statutes` does all of it. No new CLI kind or flag.
- A repeated Absatz label in one section gets its position (`-abs1f-p3`) and a warning.
- `write_jsonl` refuses to write two items with the same id.
- `parse_statute` is replaced by `read_sections`, since chunks are now cut per version.

## Alternatives
- Versions per chunk: the key breaks when a section crosses 8000 characters, and `-p{n}` ids are positional.
- Snapshot manifests with a text store: most faithful, most code.
- Start dates from "mWv" notes or a hand-kept table of amending acts: more accurate, but the notes don't say which sections they cover.
- Corrections inherit the start of the text they fix: closer to the law, but versions overlap.
- Skip list of commit SHAs: misses new broken scrapes.
- HTTP per commit: GitHub API rate limits, and 150 MB of plain files.
- New CLI kind `statutes-history`: plain `ingest` would run it anyway.

## Consequences
- `first_seen` can lag the date in force (2025-12-23 text appeared on 2025-12-24), and can be too early (§ 51 B went online early).
- Corrections that undo an amendment (§ 11) and early text (§ 51) show as separate versions.
- The `change` flag is only as good as `doknr`. 11 times a `doknr` changed with identical text.
- Versions older than 2019-06-10 are not covered.
- Depends on QuantLaw keeping the archive up to date. The 3-day warning shows when it stops.
- The first run downloads about 150 MB and may need `--extra ocr` for one old image.
- A real mass repeal of over 10% of sections would be skipped as broken (logged).
- Snapshots are the commits that change the XML. An image that changed without the XML would be missed (none has so far).
- `heading_path` comes from the first-seen snapshot. A section that only moves under a new heading keeps its version.
- `scripts/check_enwg_history.py` checks the result against facts I measured by hand. CI doesn't run it.
