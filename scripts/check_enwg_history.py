"""Check the EnWG version history against facts I measured by hand on the archive.

Run from the repository root after ``uv run lawclerk ingest statutes``:
``uv run python scripts/check_enwg_history.py``

It rebuilds the history from the local archive clone (no fetch), compares it with the
written versions file, and exits with 1 if any check fails.
"""

import itertools
import sys
from datetime import date

from lawclerk.ingest import archive, statutes

LAW, SLUG = "EnWG", "enwg_2005"
BROKEN = date(2026, 5, 5)
AFTER_BROKEN = date(2026, 5, 7)

failures = 0


def check(name: str, ok: bool, detail: object = "") -> None:
    """Print one check's result and count it if it failed."""
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if not ok else ""))


def main() -> None:
    """Run every check and print every gap in a section's history."""
    repo = statutes.ARCHIVE_DIR
    xml_file = archive.find_xml(repo, SLUG)
    snapshots = archive.snapshots(repo, xml_file)
    history = statutes.build_history(
        statutes.archive_snapshots(repo, snapshots, xml_file, LAW),
        law=LAW,
        slug=SLUG,
        xml_file=xml_file,
    )
    versions = history.versions

    def first_seen(section: str) -> list[str]:
        return [str(v.first_seen) for v in versions if v.section == section]

    written = (statutes.PROCESSED_DIR / f"{SLUG}.versions.jsonl").read_text(encoding="utf-8")
    rebuilt = "".join(v.model_dump_json() + "\n" for v in versions)
    check("rebuilt versions equal the written file", rebuilt == written)

    expected_19a = ["2019-06-10", "2020-11-06", "2024-01-05", "2025-12-24", "2026-07-29"]
    check("§ 19a has 5 versions", first_seen("§ 19a") == expected_19a, first_seen("§ 19a"))
    expected_14a = ["2019-06-10", "2023-01-01", "2023-06-25", "2025-12-24"]
    check("§ 14a has 4 versions", first_seen("§ 14a") == expected_14a, first_seen("§ 14a"))
    check("§ 2 has 1 version", len(first_seen("§ 2")) == 1, first_seen("§ 2"))

    touching = [v.id for v in versions if {v.first_seen, v.valid_to} & {BROKEN, AFTER_BROKEN}]
    check("nothing starts or ends on 2026-05-05 or 2026-05-07", not touching, touching)
    skipped = [day for day, _, _ in history.skipped]
    check("only 2026-05-05 is skipped", skipped == [BROKEN], skipped)

    open_sections = sorted(v.section for v in versions if v.valid_to is None)
    check(
        "open versions = sections in the newest snapshot", open_sections == sorted(history.newest)
    )

    old = statutes.version_at(versions, "§ 19a", date(2026, 7, 1))
    new = statutes.version_at(versions, "§ 19a", date(2026, 8, 1))
    check(
        "§ 19a on 2026-07-01 is the 2025-12-24 text",
        old is not None and str(old.first_seen) == "2025-12-24",
        old and old.id,
    )
    check(
        "§ 19a on 2026-08-01 is the 2026-07-29 text",
        new is not None and str(new.first_seen) == "2026-07-29",
        new and new.id,
    )
    check("§ 19a texts differ", old is not None and new is not None and old.text != new.text)

    print(f"{len(versions)} versions of {len({v.section for v in versions})} sections")
    print("Gaps (section absent between two versions):")
    by_section: dict[str, list[statutes.SectionVersion]] = {}
    for version in versions:
        by_section.setdefault(version.section, []).append(version)
    for section, runs in by_section.items():
        for before, after in itertools.pairwise(runs):
            if before.valid_to != after.first_seen:
                print(f"  {section}: absent {before.valid_to} to {after.first_seen}")
        if runs[-1].valid_to is not None:
            print(f"  {section}: absent since {runs[-1].valid_to}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
