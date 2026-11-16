"""Decide what changed, from the last snapshot and this crawl: no HTTP, no disk."""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, Self, TypedDict
from urllib.parse import urlsplit

from lawclerk.ingest.rulings.crawl import BrokenRunError, Crawl
from lawclerk.ingest.rulings.fetch import DATA_HOST
from lawclerk.ingest.rulings.snapshot import (
    AnyLine,
    CarriedBecause,
    FileLine,
    Link,
    PageLine,
    RowLine,
    RunHeader,
    SkippedLine,
    Snapshot,
    Tracked,
    run_time,
)

MIN_RATIO: Final = 0.9  # fewer rows or loaded pages than this share of the last good run
# Every known file is re-downloaded and hashed once per period, spread over the runs
RECHECK_PERIOD: Final = timedelta(days=90)


class Origin(TypedDict):
    """When a line was first seen, and whether that was in the first run."""

    first_seen: str
    baseline: bool


@dataclass
class Previous:
    """The last good run's lines, by key."""

    run: str | None = None
    header: RunHeader | None = None
    rows: dict[tuple[str, str], RowLine] = field(default_factory=dict)
    pages: dict[str, PageLine] = field(default_factory=dict)
    files: dict[str, FileLine] = field(default_factory=dict)
    skipped: dict[str, SkippedLine] = field(default_factory=dict)

    @classmethod
    def of(cls, snapshot: Snapshot | None) -> Self:
        """Index a snapshot's lines; an empty Previous before the first run."""
        previous = cls()
        if snapshot is None:
            return previous
        previous.run, previous.header = snapshot.header.run, snapshot.header
        for line in snapshot.lines:
            if isinstance(line, RowLine):
                previous.rows[(line.list_page, line.aktenzeichen)] = line
            elif isinstance(line, PageLine):
                previous.pages[line.url] = line
            elif isinstance(line, FileLine):
                previous.files[line.url] = line
            else:
                previous.skipped[line.url] = line
        return previous


def origin(before: Tracked | None, run: str, previous_run: str | None) -> Origin:
    """Return first_seen and baseline: kept from before, or this run's."""
    if before is None:
        return Origin(first_seen=run, baseline=previous_run is None)
    return Origin(first_seen=before.first_seen, baseline=before.baseline)


def kept_links(crawl: Crawl, links: Sequence[Link]) -> tuple[list[Link], CarriedBecause | None]:
    """Keep the earlier links whose pages didn't load this run, and say why.

    A link is dropped only when its page loaded and no longer shows its href. Nothing
    kept means gone. A link kept because its page failed, or because its page still
    shows it but its target (such as an .html redirect) failed, is page_failed;
    otherwise page_not_reached.
    """
    loaded, shown = crawl.loaded(), crawl.shown
    kept = [
        link
        for link in links
        if link.page not in loaded or link.href in shown.get(link.page, set())
    ]
    if any(link.page in crawl.pages for link in kept):
        return kept, "page_failed"
    return kept, "page_not_reached" if kept else None


def links_now(
    crawl: Crawl, before: PageLine | FileLine | SkippedLine | None, links: list[Link]
) -> list[Link]:
    """This run's links plus the earlier ones whose pages didn't load, each once."""
    kept = kept_links(crawl, before.found_on)[0] if before is not None else []
    merged: dict[tuple[str, str], Link] = {}
    for link in [*links, *kept]:
        merged.setdefault((link.page, link.href), link)
    return list(merged.values())


def unseen(crawl: Crawl, line: AnyLine, run: str) -> AnyLine:
    """Turn a line this run didn't observe into a carried or a gone line."""
    if line.seen == "gone":
        return line
    links = line.links if isinstance(line, RowLine) else line.found_on
    kept, because = kept_links(crawl, links)
    if because is None:
        return line.model_copy(update={"seen": "gone", "carried_because": None, "gone_since": run})
    found = {} if isinstance(line, RowLine) else {"found_on": kept}
    return line.model_copy(update={"seen": "carried", "carried_because": because, **found})


def check_counts(result: Crawl, previous: Previous) -> BrokenRunError | None:
    """Return why the run is broken if rows or loaded pages fell below MIN_RATIO of the
    last run, or None.

    Pages count against the last snapshot's pages that were observed or carried because a
    page failed, so a run of partial runs can't lower the bar. Pages no link leads to any
    more are left out, or they would raise it forever.
    """
    if previous.header is None:
        return None
    listed = sum(
        page.seen == "observed" or page.carried_because == "page_failed"
        for page in previous.pages.values()
    )
    rows, pages = len(result.rows), len(result.loaded())
    if rows < MIN_RATIO * previous.header.rows:
        return BrokenRunError(
            "too_few_rows", f"{rows} list rows, the last good run had {previous.header.rows}"
        )
    if pages < MIN_RATIO * listed:
        return BrokenRunError(
            "too_few_pages",
            f"{pages} pages loaded, the last snapshot has {listed} observed or failed",
        )
    return None


def rotation(previous: Previous, urls: Iterable[str], run: str) -> set[str]:
    """Pick the known files to re-download this run, longest-unchecked first.

    The share is the time since the last good run over RECHECK_PERIOD (1 day: 1/90, at most
    all), rounded down, so running often adds no load and running rarely covers everything.
    """
    if previous.run is None:
        return set()
    known = sorted((previous.files[url].checked, url) for url in urls if url in previous.files)
    share = min(1.0, (run_time(run) - run_time(previous.run)) / RECHECK_PERIOD)
    return {url for _, url in known[: math.floor(len(known) * share)]}


def to_download(
    previous: Previous,
    crawl: Crawl,
    vs: dict[str, str | None],
    resumed: dict[str, FileLine],
    run: str,
) -> dict[str, str | None]:
    """Return the files to request, each with the ETag to ask If-None-Match with, or None.

    Downloaded: new, with another v, or due in the rotation. A known data-host file with an
    ETag is asked with it. A file a crashed run already stored at the same v, or one
    skipped as video or too large before, is not requested again.
    """
    due = rotation(previous, crawl.files, run)
    urls: dict[str, str | None] = {}
    for url in crawl.files:
        before = previous.files.get(url)
        skipped = previous.skipped.get(url)
        if url in resumed and resumed[url].v == vs[url]:
            continue
        if before is None and skipped is not None and skipped.reason in ("video", "too_large"):
            continue
        if before is None or before.v != vs[url] or url in due:
            urls[url] = None
        elif urlsplit(url).netloc == DATA_HOST and before.etag:
            urls[url] = before.etag
    return urls
