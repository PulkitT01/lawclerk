"""Fetch the BK6 documents of the Bundesnetzagentur into data/raw/rulings/, one snapshot per run.

Each run crawls the two BK6 lists and every page they lead to inside the boundary, downloads
only what is new or changed, and writes runs/<run>.jsonl. Nothing stored is overwritten or
deleted: a changed file is stored beside the old one. See ADR 0004.

Run from the repository root: ``uv run lawclerk ingest rulings``
"""

import logging
import math
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Self, TypedDict
from urllib.parse import urlsplit

import httpx

from lawclerk.ingest.rulings.crawl import BrokenRunError, Crawl, crawl
from lawclerk.ingest.rulings.fetch import (
    DATA_HOST,
    MAX_CONCURRENT,
    SITE,
    FetchError,
    FirewallError,
    make_client,
    query_v,
    server_v,
)
from lawclerk.ingest.rulings.parse import main_sha256, size_from_text
from lawclerk.ingest.rulings.snapshot import (
    RUNS,
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
    journal_path,
    last_good_snapshot,
    read_journals,
    run_id,
    run_time,
    write_snapshot,
)
from lawclerk.ingest.rulings.store import TMP, Download, download, store_bytes

logger = logging.getLogger(__name__)

ROOT: Final = Path("data/raw/rulings")


MIN_RATIO: Final = 0.9  # fewer rows or loaded pages than this share of the last good run
# Every known file is re-downloaded and hashed once per period, spread over the runs
RECHECK_PERIOD: Final = timedelta(days=90)


# --- One run -------------------------------------------------------------------------------


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


@dataclass
class Run:
    """One ingest run: its id, the last good run, and the lines it has written so far."""

    client: httpx.Client
    root: Path
    run: str
    previous: Previous
    crawl: Crawl
    lines: list[AnyLine] = field(default_factory=list)


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


def ingest(
    client: httpx.Client,
    root: Path = ROOT,
    now: datetime | None = None,
    announce: Callable[[str], None] = print,
) -> RunHeader:
    """Run once: crawl, compare with the last good snapshot, download, write a snapshot.

    A broken run writes a snapshot with only its header and keeps its journal, so the
    files it stored are reused by the next run.
    """
    run = run_id(now or datetime.now(UTC))
    (root / RUNS).mkdir(parents=True, exist_ok=True)
    for leftover in (root / TMP).glob("*"):
        leftover.unlink()  # scratch from a crashed run; stored copies are never in here
    previous = Previous.of(last_good_snapshot(root))
    try:
        result = crawl(client)
        broken = check_counts(result, previous)
        if broken is not None:
            raise broken
        state = Run(client, root, run, previous, result)
        record_rows(state)
        record_pages(state)
        counts = record_files(state, announce)
        record_skipped(state)
    except BrokenRunError as exc:
        header = RunHeader(
            run=run, status="broken", broken_rule=exc.rule, reason=str(exc), previous=previous.run
        )
        write_snapshot(root, Snapshot(header, []))
        return header
    return finish(state, counts)


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


def record_rows(state: Run) -> None:
    """Write a line per list row: observed, or gone from its list."""
    seen: set[tuple[str, str]] = set()
    for list_page, row in state.crawl.rows:
        aktenzeichen = row.cells[0] if row.cells else ""
        key = (list_page, aktenzeichen)
        seen.add(key)
        state.lines.append(
            RowLine(
                seen="observed",
                list_page=list_page,
                aktenzeichen=aktenzeichen,
                cells=row.cells,
                links=[Link(page=list_page, text=text, href=href) for href, text in row.links],
                **origin(state.previous.rows.get(key), state.run, state.previous.run),
            )
        )
    state.lines += [
        unseen(state.crawl, line, state.run)
        for key, line in state.previous.rows.items()
        if key not in seen
    ]


def record_pages(state: Run) -> None:
    """Write a line per page; store a new copy when its <main> changed."""
    for url, page in state.crawl.pages.items():
        before = state.previous.pages.get(url)
        found_on = links_now(state.crawl, before, page.found_on)
        if page.parsed is None or page.body is None:
            update = {"seen": "carried", "carried_because": "page_failed", "found_on": found_on}
            state.lines.append(
                before.model_copy(update=update)
                if before is not None
                else PageLine(
                    seen="carried",
                    carried_because="page_failed",
                    url=url,
                    depth=page.depth,
                    found_on=found_on,
                    checked=None,
                    main_sha256=None,
                    stored=None,
                    **origin(None, state.run, state.previous.run),
                )
            )
            continue
        main = main_sha256(page.body)
        stored = before.stored if before is not None else None
        if stored is None or before is None or before.main_sha256 != main:
            stored = store_bytes(state.root, url, page.body, state.run)
        state.lines.append(
            PageLine(
                seen="observed",
                url=url,
                depth=page.depth,
                found_on=found_on,
                checked=state.run,
                main_sha256=main,
                stored=stored,
                **origin(before, state.run, state.previous.run),
            )
        )
    for url, line in state.previous.pages.items():
        if url not in state.crawl.pages:
            # A page nobody reached is never gone: its links are carried with it
            kept, because = kept_links(state.crawl, line.found_on)
            update = {"carried_because": because or "page_not_reached", "found_on": kept}
            state.lines.append(line.model_copy(update={"seen": "carried", **update}))


@dataclass
class FileCounts:
    """Counts from the file step, for the header."""

    stored: int = 0
    checked: int = 0
    carried: int = 0
    failed: int = 0
    v_differs: int = 0
    v_fallback: int = 0
    changed_same_v: int = 0
    not_modified: int = 0


def link_v(links: Sequence[Link]) -> str | None:
    """Return the v the pages show for a file: that of the first link that has one."""
    return next((v for link in links if (v := query_v(link.href)) is not None), None)


def current_vs(state: Run, counts: FileCounts) -> dict[str, str | None]:
    """Find each file's current v: from its redirect, or by asking the site.

    Data-host files have no v. If the firewall objects to these extra requests, the rest
    use the v their links show.
    """
    files = state.crawl.files
    vs: dict[str, str | None] = {}
    ask: list[str] = []
    for url in files:
        if url in state.crawl.redirect_v:
            vs[url] = state.crawl.redirect_v[url]
        elif urlsplit(url).netloc == SITE:
            ask.append(url)
        else:
            vs[url] = None
    stop = threading.Event()

    def ask_site(url: str) -> str | None:
        if stop.is_set():
            return None
        try:
            return server_v(state.client, url)
        except FirewallError:
            stop.set()
        except FetchError:
            pass
        return None

    with ThreadPoolExecutor(MAX_CONCURRENT) as pool:
        for url, v in zip(ask, pool.map(ask_site, ask), strict=True):
            if v is None:
                counts.v_fallback += 1
                v = link_v(files[url])
            vs[url] = v
    if stop.is_set():
        logger.warning("rulings: the firewall rejected a v check; the rest used the links' v")
    for url, links in files.items():
        shown = {query_v(link.href) for link in links} - {None}
        counts.v_differs += vs[url] is not None and bool(shown) and shown != {vs[url]}
    return vs


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


def fetch_url(url: str, v: str | None) -> str:
    """Return the URL to download a file from: on www with the site's query and v."""
    if v is not None and urlsplit(url).netloc == SITE:
        return f"{url}?__blob=publicationFile&v={v}"
    return url


def record_files(state: Run, announce: Callable[[str], None]) -> FileCounts:
    """Download what is new, changed or due for a re-check; carry everything else."""
    counts = FileCounts()
    vs = current_vs(state, counts)
    resumed = read_journals(state.root, state.previous.run)
    urls = to_download(state.previous, state.crawl, vs, resumed, state.run)
    if state.previous.run is None:
        announce(expected_size(state.crawl, list(urls)))

    downloads: dict[str, Download] = {}
    journal = journal_path(state.root, state.run)
    with ThreadPoolExecutor(MAX_CONCURRENT) as pool, journal.open("a", encoding="utf-8") as log:

        def get_file(url: str) -> Download:
            target = fetch_url(url, vs[url])
            return download(state.client, state.root, target, state.run, urls[url])

        # Journal each file as soon as it is stored, so a kill loses at most one line
        futures = {pool.submit(get_file, url): url for url in urls}
        try:
            for future in as_completed(futures):
                url, done = futures[future], future.result()
                downloads[url] = done
                if done.stored is not None:
                    log.write(file_line(state, url, done, vs[url]).model_dump_json() + "\n")
                    log.flush()
        except FirewallError as exc:
            pool.shutdown(cancel_futures=True)
            raise BrokenRunError("firewall", f"firewall rejected {exc}") from exc

    for url, links in state.crawl.files.items():
        state.lines.append(
            file_or_skipped(state, url, links, downloads.get(url), resumed, vs, counts)
        )
    state.lines += [
        unseen(state.crawl, line, state.run)
        for url, line in state.previous.files.items()
        if url not in state.crawl.files
    ]
    return counts


def file_or_skipped(
    state: Run,
    url: str,
    links: list[Link],
    done: Download | None,
    resumed: dict[str, FileLine],
    vs: dict[str, str | None],
    counts: FileCounts,
) -> FileLine | SkippedLine:
    """Build the line of a file seen this run, from its download or from earlier runs."""
    before = state.previous.files.get(url)
    skipped = state.previous.skipped.get(url)
    found_on = links_now(state.crawl, before or skipped, links)
    observed = {
        "seen": "observed",
        "carried_because": None,
        "gone_since": None,
        "found_on": found_on,
    }
    if done is not None and done.stored is not None:
        line = file_line(state, url, done, vs[url])
        counts.checked += 1
        counts.stored += line.stored.run == state.run
        if before is not None and before.v == vs[url]:
            counts.changed_same_v += before.stored.sha256 != line.stored.sha256
        return line.model_copy(update={"found_on": found_on})
    if done is not None and done.not_modified and before is not None:
        counts.carried += 1
        counts.not_modified += 1
        return before.model_copy(update={**observed, "hash": "carried"})
    if done is not None and done.skipped is not None:
        return SkippedLine(
            seen="observed",
            url=url,
            found_on=found_on,
            reason=done.skipped,
            content_type=done.content_type,
            bytes=done.bytes,
            checked=state.run,
            **origin(skipped or before, state.run, state.previous.run),
        )
    if done is not None:
        counts.failed += 1  # a failed re-check keeps the old copy; a failed new file is skipped
    if url in resumed and resumed[url].v == vs[url]:
        counts.carried += 1
        return resumed[url].model_copy(update={**observed, "hash": "carried"})
    if before is not None:
        counts.carried += 1
        return before.model_copy(update={**observed, "hash": "carried"})
    if skipped is not None and done is None:
        return skipped.model_copy(update=observed)  # video or too large, not requested again
    return SkippedLine(
        seen="observed",
        url=url,
        found_on=found_on,
        reason="failed",
        checked=state.run,
        **origin(skipped, state.run, state.previous.run),
    )


def file_line(state: Run, url: str, done: Download, v: str | None) -> FileLine:
    """Build the line of a file this run downloaded; unchanged content keeps its old copy."""
    assert done.stored is not None
    before = state.previous.files.get(url)
    unchanged = before is not None and before.stored.sha256 == done.stored.sha256
    return FileLine(
        seen="observed",
        url=url,
        found_on=state.crawl.files.get(url, []),
        v=v,
        hash="checked",
        checked=state.run,
        content_type=done.content_type,
        bytes=done.bytes,
        stored=before.stored if before is not None and unchanged else done.stored,
        etag=done.etag,
        last_modified=done.last_modified,
        **origin(before, state.run, state.previous.run),
    )


def expected_size(crawl: Crawl, urls: Sequence[str]) -> str:
    """Say what a first download will take, from the sizes in the link texts."""
    sizes = [
        next((size for link in crawl.files[url] if (size := size_from_text(link.text))), None)
        for url in urls
    ]
    known = [size for size in sizes if size is not None]
    return (
        f"rulings: first download, {len(urls)} files: at least {sum(known) / 1e6:.0f} MB, "
        f"{len(sizes) - len(known)} of them of unknown size"
    )


def record_skipped(state: Run) -> None:
    """Write a line per off-site or out-of-boundary link; carry or retire earlier ones."""
    for url, (reason, links) in state.crawl.skipped.items():
        before = state.previous.skipped.get(url)
        state.lines.append(
            SkippedLine(
                seen="observed",
                url=url,
                found_on=links_now(state.crawl, before, links),
                reason=reason,
                **origin(before, state.run, state.previous.run),
            )
        )
    written = {line.url for line in state.lines if isinstance(line, SkippedLine | FileLine)}
    state.lines += [
        unseen(state.crawl, line, state.run)
        for url, line in state.previous.skipped.items()
        if url not in written
    ]


def finish(state: Run, counts: FileCounts) -> RunHeader:
    """Write the snapshot with its header. The journal stays; later runs ignore it."""
    pages = state.crawl.pages
    failed = sum(page.parsed is None for page in pages.values())
    lines = state.lines
    checks = [line.checked for line in lines if isinstance(line, FileLine) and line.seen != "gone"]
    skipped = Counter(
        line.reason for line in lines if isinstance(line, SkippedLine) and line.seen != "gone"
    )
    header = RunHeader(
        run=state.run,
        status="partial" if failed or counts.failed else "ok",
        previous=state.previous.run,
        rows=len(state.crawl.rows),
        pages_loaded=len(pages) - failed,
        pages_failed=failed,
        pages_by_depth=state.crawl.by_depth(),
        files_stored=counts.stored,
        files_checked=counts.checked,
        files_carried=counts.carried,
        files_not_modified=counts.not_modified,
        files_failed=counts.failed,
        skipped=dict(sorted(skipped.items())),
        gone=sum(line.gone_since == state.run for line in lines),
        v_differs=counts.v_differs,
        v_fallback=counts.v_fallback,
        changed_same_v=counts.changed_same_v,
        oldest_check=min(checks, default=None),
    )
    write_snapshot(state.root, Snapshot(header, lines))
    return header


def summary(header: RunHeader, now: datetime) -> str:
    """Return a run's summary for the terminal."""
    age = ""
    if header.oldest_check is not None:
        age = f"; oldest check {(now - run_time(header.oldest_check)).days} days ago"
    reason = f" ({header.reason})" if header.reason else ""
    return (
        f"rulings: {header.status}{reason}; {header.rows} rows; pages {header.pages_loaded} "
        f"loaded, {header.pages_failed} failed, by depth {header.pages_by_depth}; files "
        f"{header.files_stored} new, {header.files_checked} checked, {header.files_carried} "
        f"carried ({header.files_not_modified} by ETag), {header.files_failed} failed; "
        f"skipped {header.skipped}; {header.gone} gone; "
        f"v differs on {header.v_differs}, links' v used for {header.v_fallback}, "
        f"new content under the same v {header.changed_same_v}{age}"
    )


def main() -> None:
    """Fetch the BK6 documents into ROOT. Exits 1 on a broken run, 2 on a partial one."""
    with make_client() as client:
        header = ingest(client)
    print(summary(header, datetime.now(UTC)))
    if header.status == "broken":
        raise SystemExit(f"rulings: broken run, nothing recorded as gone: {header.reason}")
    if header.status == "partial":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
