"""Crawl the BK6 site like ``lawclerk ingest rulings`` would, and only count. Downloads nothing.

Run from the repository root: ``uv run python scripts/rulings_dry_run.py``

Prints pages per depth, pages, files and MB per top folder, file links by host and type,
and the expected download size from the link texts. Exits with 1 if a loaded page lacks a
canonical link or <main>, a list row doesn't have 4 cells, or a URL can't be stored.
"""

import sys
from collections import Counter
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from lawclerk.ingest import rulings
from lawclerk.ingest.rulings import fetch

failures = 0
GZ, TOPICS = rulings.BOUNDARY


def top_folder(url: str) -> str:
    """Return the top folder of a URL: BK6-GZ, BK06/<folder>, or its host for anything else."""
    path = urlsplit(url).path.removeprefix("/Bundesnetzagentur")  # the data host's prefix
    if path.startswith(GZ):
        return "BK6-GZ"
    if path.startswith(TOPICS):
        first, slash, _ = path.removeprefix(TOPICS).partition("/")
        return f"BK06/{first}" if slash else "BK06"
    return urlsplit(url).netloc


def check(name: str, problems: list[str]) -> None:
    """Print one check's result, with up to 10 of its problems, and count it if it failed."""
    global failures
    failures += bool(problems)
    print(f"{'FAIL' if problems else 'PASS'} {name}" + (f": {len(problems)}" if problems else ""))
    for problem in problems[:10]:
        print(f"    {problem}")


def main() -> None:
    """Crawl, print the counts and run the checks."""
    with fetch.make_client() as client:
        result = rulings.crawl(client)
    pages = result.pages
    loaded = result.loaded()
    print(f"pages: {len(loaded)} loaded, {len(pages) - len(loaded)} failed")
    print(f"pages by depth: {result.by_depth()}")
    for url, page in pages.items():
        if page.parsed is None:
            print(f"    failed: {url} ({page.failed})")
    print(f"rows: {Counter(list_page for list_page, _ in result.rows)}")

    folder_pages = Counter(top_folder(url) for url in loaded)
    folder_files: Counter[str] = Counter()
    folder_bytes: Counter[str] = Counter()
    by_type: Counter[tuple[str, str]] = Counter()
    sizes: Counter[tuple[str, str]] = Counter()
    unknown: Counter[tuple[str, str]] = Counter()
    for url, links in result.files.items():
        parts = urlsplit(url)
        kind = (parts.netloc, PurePosixPath(parts.path).suffix.lower() or "-")
        by_type[kind] += 1
        size = next((s for link in links if (s := rulings.size_from_text(link.text))), None)
        folder_files[top_folder(url)] += 1
        folder_bytes[top_folder(url)] += size or 0
        if size is None:
            unknown[kind] += 1
        else:
            sizes[kind] += size
    print(
        f"file links: {len(result.files)}, {len(result.redirect_v)} of them via an .html redirect"
    )
    for kind, count in by_type.most_common():
        print(
            f"    {kind[0]} {kind[1]}: {count}, {sizes[kind] / 1e6:.0f} MB, {unknown[kind]} unknown"
        )
    print("per top folder (pages, files, MB from link texts):")
    for folder in sorted(folder_pages.keys() | folder_files.keys()):
        print(
            f"    {folder}: {folder_pages[folder]} pages, {folder_files[folder]} files, "
            f"{folder_bytes[folder] / 1e6:.0f} MB"
        )
    print(
        f"expected size: at least {sum(sizes.values()) / 1e6:.0f} MB, "
        f"{sum(unknown.values())} files of unknown size"
    )
    print(f"skipped links: {Counter(reason for reason, _ in result.skipped.values())}")
    for url, (reason, links) in sorted(result.skipped.items()):
        if reason == "outside_boundary":
            print(f"    outside: {url} ({len(links)} links)")

    check(
        "every loaded page has a canonical link",
        [url for url, page in loaded.items() if page.parsed and not page.parsed.canonical],
    )
    check(
        "every page has <main>", [url for url, page in pages.items() if page.failed == "no <main>"]
    )
    check(
        "every list row has 4 cells",
        [f"{list_page}: {row.cells}" for list_page, row in result.rows if len(row.cells) != 4],
    )
    check(
        "every URL can be stored under the root",
        [url for url, (reason, _) in result.skipped.items() if reason == "unsafe_path"],
    )
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
