"""Fetch the BK6 documents of the Bundesnetzagentur into data/raw/rulings/, one snapshot per run.

Each run crawls the two BK6 lists and every page they lead to inside the boundary, downloads
only what is new or changed, and writes runs/<run>.jsonl. Nothing stored is overwritten or
deleted: a changed file is stored beside the old one. See ADR 0004.

Run from the repository root: ``uv run lawclerk ingest rulings``
"""

import hashlib
import logging
import math
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, Literal, Self, TypedDict
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from lawclerk.ingest.rulings.fetch import (
    DATA_HOST,
    FIREWALL_MARKER,
    HOSTS,
    MAX_CONCURRENT,
    SITE,
    FetchError,
    FirewallError,
    get,
    is_overloaded,
    is_page_path,
    make_client,
    query_v,
    server_v,
    url_key,
    with_retries,
)
from lawclerk.ingest.rulings.parse import PageParser, Row, main_sha256, parse_page, size_from_text

logger = logging.getLogger(__name__)

ROOT: Final = Path("data/raw/rulings")
RUNS: Final = "runs"  # snapshot folder inside the root
TMP: Final = ".tmp"  # unfinished downloads inside the root

LISTS: Final = (
    f"https://{SITE}/DE/Beschlusskammern/BK06/BK6_21_AV/BK6_AV.html",  # closed proceedings
    f"https://{SITE}/DE/Beschlusskammern/BK06/BK6_11_LV/BK6_LV.html",  # ongoing proceedings
)
# The crawl follows every page below these: BK6's proceedings and BK6's topic pages
BOUNDARY: Final = ("/DE/Beschlusskammern/1_GZ/BK6-GZ/", "/DE/Beschlusskammern/BK06/")


MAX_PAGES: Final = 2000  # more pages than this means the crawl ran away: broken run
MAX_FILE_BYTES: Final = 100_000_000
MIN_RATIO: Final = 0.9  # fewer rows or loaded pages than this share of the last good run
# Every known file is re-downloaded and hashed once per period, spread over the runs
RECHECK_PERIOD: Final = timedelta(days=90)
FORMAT_VERSION: Final = 1


# --- Snapshot lines ------------------------------------------------------------------------


class Link(BaseModel):
    """One link as a page showed it: the page, the link text, and the href exactly as written."""

    page: str
    text: str
    href: str


class Stored(BaseModel):
    """A copy on disk, relative to the root, and the run since which this content is current."""

    path: str
    sha256: str
    run: str


Seen = Literal["observed", "carried", "gone"]
CarriedBecause = Literal["page_failed", "page_not_reached"]
BrokenRule = Literal["list_failed", "firewall", "page_cap", "too_few_rows", "too_few_pages"]
SkipReason = Literal["video", "too_large", "off_site", "outside_boundary", "unsafe_path", "failed"]


class Tracked(BaseModel):
    """Fields every line but the header has.

    seen: observed by this run, carried over from the last good run, or gone (every page
    that showed it loaded and no longer does). first_seen is the run that first saw it, not
    the publication date; baseline marks what the first run found.
    """

    seen: Seen
    carried_because: CarriedBecause | None = None
    gone_since: str | None = None
    first_seen: str
    baseline: bool


class RowLine(Tracked):
    """One row of a list page: its cells as text and the links in it."""

    kind: Literal["row"] = "row"
    list_page: str
    aktenzeichen: str
    cells: list[str]
    links: list[Link]


class PageLine(Tracked):
    """One HTML page. A new copy is stored only when its <main> changed."""

    kind: Literal["page"] = "page"
    url: str
    depth: int
    found_on: list[Link]
    checked: str | None  # the last run that loaded it
    main_sha256: str | None
    stored: Stored | None


class FileLine(Tracked):
    """One downloaded file. hash says whether this run downloaded and hashed it itself."""

    kind: Literal["file"] = "file"
    url: str
    found_on: list[Link]
    v: str | None  # the site's v from the redirect, or the links' v as a fallback
    hash: Literal["checked", "carried"]
    checked: str  # the last run that downloaded and hashed it
    content_type: str | None
    bytes: int
    stored: Stored
    etag: str | None = None  # data host only: asked with If-None-Match each run
    last_modified: str | None = None  # data host only, for the record


class SkippedLine(Tracked):
    """A link that was not downloaded or not followed, and why."""

    kind: Literal["skipped"] = "skipped"
    url: str
    found_on: list[Link]
    reason: SkipReason
    content_type: str | None = None
    bytes: int | None = None
    checked: str | None = None  # the run that requested it, for video, too_large and failed


AnyLine = RowLine | PageLine | FileLine | SkippedLine
LINES: Final = TypeAdapter[AnyLine](Annotated[AnyLine, Field(discriminator="kind")])


class RunHeader(BaseModel):
    """The first line of a snapshot: what kind of run it was, and its counts."""

    kind: Literal["run"] = "run"
    format: int = FORMAT_VERSION
    run: str
    status: Literal["ok", "partial", "broken"]
    broken_rule: BrokenRule | None = None
    reason: str | None = None
    previous: str | None  # the last good run this one was compared with
    rows: int = 0
    pages_loaded: int = 0
    pages_failed: int = 0
    pages_by_depth: dict[str, int] = Field(default_factory=dict)
    files_stored: int = 0  # new content stored this run
    files_checked: int = 0  # downloaded and hashed this run
    files_carried: int = 0
    files_not_modified: int = 0  # data-host files whose ETag answered 304 (counted as carried)
    files_failed: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    gone: int = 0  # lines that became gone in this run
    v_differs: int = 0  # files whose links show another v than the site's
    v_fallback: int = 0  # files that used the links' v because the site's wasn't known
    changed_same_v: int = 0  # re-checks that found new content under an unchanged v
    oldest_check: str | None = None  # the oldest last download among the files


@dataclass
class Snapshot:
    """A snapshot file: its header and every other line."""

    header: RunHeader
    lines: list[AnyLine]


RUN_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


def run_id(now: datetime) -> str:
    """Return the id of a run started at now, e.g. 2026-11-16T09:00:00Z."""
    return now.astimezone(UTC).strftime(RUN_FORMAT)


def run_time(run: str) -> datetime:
    """Return the start of a run from its id."""
    return datetime.strptime(run, RUN_FORMAT).replace(tzinfo=UTC)


def snapshot_path(root: Path, run: str) -> Path:
    """Return the snapshot file of a run; colons don't belong in file names."""
    return root / RUNS / f"{run.replace(':', '-')}.jsonl"


def journal_path(root: Path, run: str) -> Path:
    """Return a run's journal: the files it stored, one line each, written as it goes."""
    return root / RUNS / f"{run.replace(':', '-')}.journal.jsonl"


def read_snapshot(path: Path) -> Snapshot:
    """Read a snapshot file. Refuse one in a format this code doesn't know."""
    first, *rest = path.read_text(encoding="utf-8").splitlines()
    header = RunHeader.model_validate_json(first)
    if header.format != FORMAT_VERSION:
        raise ValueError(f"{path}: snapshot format {header.format}, expected {FORMAT_VERSION}")
    return Snapshot(header, [LINES.validate_json(line) for line in rest])


def last_good_snapshot(root: Path) -> Snapshot | None:
    """Return the newest snapshot of a run that wasn't broken, or None before the first."""
    for path in sorted((root / RUNS).glob("*Z.jsonl"), reverse=True):
        snapshot = read_snapshot(path)
        if snapshot.header.status != "broken":
            return snapshot
    return None


def write_snapshot(root: Path, snapshot: Snapshot) -> Path:
    """Write a snapshot file in one step, so a crash never leaves half of one."""
    path = snapshot_path(root, snapshot.header.run)
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [snapshot.header, *snapshot.lines]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(line.model_dump_json() + "\n" for line in lines), encoding="utf-8")
    tmp.replace(path)
    return path


def read_journals(root: Path, after: str | None) -> dict[str, FileLine]:
    """Read the files stored by runs that crashed or broke after the last good run."""
    newest_good = snapshot_path(root, after).name.removesuffix(".jsonl") if after else ""
    cache: dict[str, FileLine] = {}
    for path in sorted((root / RUNS).glob("*.journal.jsonl")):
        if path.name.removesuffix(".journal.jsonl") <= newest_good:
            continue
        for text in path.read_text(encoding="utf-8").splitlines():
            try:
                line = FileLine.model_validate_json(text)
            except ValidationError:
                continue  # the last line of a crashed run may be cut off
            if (root / line.stored.path).is_file():
                cache[line.url] = line
    return cache


# --- URLs and paths ------------------------------------------------------------------------


def in_boundary(path: str) -> bool:
    """Tell whether the crawl follows a page path: anywhere below BK6-GZ or BK06."""
    return path.startswith(BOUNDARY)


def resource_dir(url: str) -> PurePosixPath:
    """Map a URL to its folder relative to the root: <host>/<decoded path>.

    Each URL gets its own folder; each version of its content is a file in it. Raises
    ValueError for a host we don't store or a path segment that could leave the folder.
    """
    parts = urlsplit(url)
    if parts.netloc not in HOSTS:
        raise ValueError(f"not a stored host: {url}")
    segments = [unquote(segment) for segment in parts.path.split("/")[1:]]
    for segment in segments:
        if not segment or segment.startswith(".") or any(c in segment for c in "/\\\0"):
            raise ValueError(f"unsafe path segment {segment!r} in {url}")
    return PurePosixPath(parts.netloc, *segments)


def is_safe(url: str) -> bool:
    """Tell whether a URL on our hosts can be stored (see resource_dir)."""
    try:
        resource_dir(url)
    except ValueError:
        return False
    return True


# --- HTTP ----------------------------------------------------------------------------------


class BrokenRunError(Exception):
    """The crawl can't be trusted, so the run records nothing as gone."""

    def __init__(self, rule: BrokenRule, reason: str) -> None:
        super().__init__(reason)
        self.rule: BrokenRule = rule


# --- Crawl ---------------------------------------------------------------------------------


@dataclass
class Page:
    """One page this run tried to load."""

    url: str
    depth: int
    found_on: list[Link]
    body: bytes | None = None
    location: str | None = None  # where it redirected to
    failed: str | None = None
    parsed: PageParser | None = None


@dataclass
class Crawl:
    """What one crawl saw: pages, file links, skipped links and list rows."""

    pages: dict[str, Page] = field(default_factory=dict)
    files: dict[str, list[Link]] = field(default_factory=dict)
    redirect_v: dict[str, str | None] = field(default_factory=dict)  # file -> v of its redirect
    skipped: dict[str, tuple[SkipReason, list[Link]]] = field(default_factory=dict)
    rows: list[tuple[str, Row]] = field(default_factory=list)  # (list page, row)
    shown: dict[str, set[str]] = field(default_factory=dict)  # loaded page -> hrefs it shows

    def loaded(self) -> dict[str, Page]:
        """Return the pages that loaded and were parsed."""
        return {url: page for url, page in self.pages.items() if page.parsed is not None}

    def by_depth(self) -> dict[str, int]:
        """Count the loaded pages per depth."""
        depths = Counter(page.depth for page in self.loaded().values())
        return {str(depth): count for depth, count in sorted(depths.items())}


def fetch_page(client: httpx.Client, page: Page) -> Page:
    """Load one page. A page without <main> counts as failed: its links can't be read."""
    try:
        response = get(client, page.url)
    except FetchError as exc:
        page.failed = str(exc)
        return page
    if response.is_redirect:
        page.location = urljoin(page.url, response.headers.get("location", ""))
    elif response.status_code != 200:
        page.failed = f"HTTP {response.status_code}"
    else:
        page.body = response.content
        parsed = parse_page(response.content)
        if parsed.has_main:
            page.parsed = parsed
        else:
            page.failed = "no <main>"
    return page


def crawl(client: httpx.Client, max_pages: int = MAX_PAGES) -> Crawl:
    """Crawl both lists and every page inside the boundary that they lead to.

    The boundary is every page under BOUNDARY. Raises BrokenRunError when a list fails,
    the firewall answers or the crawl reaches more than max_pages pages.
    """
    result = Crawl()
    seen: set[str] = set(LISTS)
    frontier = [Page(url, 0, []) for url in LISTS]
    with ThreadPoolExecutor(MAX_CONCURRENT) as pool:
        while frontier:
            try:
                loaded = list(pool.map(lambda page: fetch_page(client, page), frontier))
            except FirewallError as exc:
                raise BrokenRunError("firewall", f"firewall rejected {exc}") from exc
            pending: dict[str, Page] = {}
            for page in loaded:
                if page.depth == 0 and page.parsed is None:
                    raise BrokenRunError(
                        "list_failed", f"list page failed: {page.url} ({page.failed})"
                    )
                if page.location is not None:
                    add_redirect(result, page, pending, seen)
                else:
                    result.pages[page.url] = page
                if page.depth == 0:
                    add_rows(result, page)
            for page in loaded:
                if page.parsed is not None:
                    add_links(result, page, pending, seen)
            if len(seen) > max_pages:
                raise BrokenRunError("page_cap", f"more than {max_pages} pages")
            frontier = list(pending.values())
    return result


def base_url(page: Page) -> str:
    """Return the URL a page's relative links resolve against (BNetzA has <base href="/">)."""
    base = page.parsed.base if page.parsed is not None else None
    return urljoin(page.url, base) if base else page.url


def add_rows(result: Crawl, page: Page) -> None:
    """Take a list page's rows.

    Raises ValueError if the list has an Aktenzeichen twice, so no row is dropped.
    """
    assert page.parsed is not None
    counts = Counter(row.cells[0] if row.cells else "" for row in page.parsed.rows)
    twice = sorted(key for key, count in counts.items() if count > 1)
    if twice:
        raise ValueError(f"{page.url}: Aktenzeichen listed more than once: {twice}")
    result.rows += [(page.url, row) for row in page.parsed.rows]


def page_links(page: Page) -> Iterator[tuple[str, Link]]:
    """Yield (absolute URL, link) for the links of a loaded page.

    A list page's links are its rows' links; any other page's are those inside <main>.
    """
    assert page.parsed is not None
    if page.depth == 0:
        pairs = [link for row in page.parsed.rows for link in row.links]
    else:
        pairs = page.parsed.links
    base = base_url(page)
    for href, text in pairs:
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base, href)
        if urlsplit(url).scheme in ("http", "https"):
            yield url, Link(page=page.url, text=text, href=href)


def add_page_link(
    result: Crawl, key: str, link: Link, depth: int, pending: dict[str, Page], seen: set[str]
) -> None:
    """Queue a page inside the boundary, or add the link to the page it leads to."""
    if key in pending:
        pending[key].found_on.append(link)
    elif key in result.pages:
        result.pages[key].found_on.append(link)
    elif key not in seen:
        seen.add(key)
        pending[key] = Page(key, depth, [link])


def add_links(result: Crawl, page: Page, pending: dict[str, Page], seen: set[str]) -> None:
    """Sort the links of a loaded page into pages to crawl, files and skipped links."""
    shown = result.shown.setdefault(page.url, set())
    for url, link in page_links(page):
        shown.add(link.href)
        key = url_key(url)
        parts = urlsplit(key)
        if key == page.url:
            continue  # a jump within the page
        is_file = not is_page_path(parts.path)
        if parts.netloc not in HOSTS:
            add_skipped(result, key, "off_site", [link])
        elif not is_file and (parts.netloc != SITE or not in_boundary(parts.path)):
            add_skipped(result, key, "outside_boundary", [link])
        elif not is_safe(key):
            add_skipped(result, key, "unsafe_path", [link])
        elif is_file:
            result.files.setdefault(key, []).append(link)
        else:
            add_page_link(result, key, link, page.depth + 1, pending, seen)


def add_redirect(result: Crawl, page: Page, pending: dict[str, Page], seen: set[str]) -> None:
    """Resolve a page link that redirected. To a file, its links become that file's links,
    keyed by the target, with the target's v."""
    assert page.location is not None
    key = url_key(page.location)
    parts = urlsplit(key)
    is_file = parts.netloc in HOSTS and not is_page_path(parts.path)
    inside = parts.netloc == SITE and in_boundary(parts.path)
    if (is_file or inside) and not is_safe(key):
        add_skipped(result, key, "unsafe_path", page.found_on)
    elif is_file:
        result.files.setdefault(key, []).extend(page.found_on)
        result.redirect_v[key] = query_v(page.location)
    elif inside:
        for link in page.found_on:
            add_page_link(result, key, link, page.depth, pending, seen)
    else:
        add_skipped(result, key, "outside_boundary", page.found_on)


def add_skipped(result: Crawl, key: str, reason: SkipReason, links: list[Link]) -> None:
    """Record links that won't be downloaded or followed."""
    result.skipped.setdefault(key, (reason, []))[1].extend(links)


# --- Files ---------------------------------------------------------------------------------


@dataclass
class Download:
    """The outcome of one download: a stored copy, or why there is none."""

    url: str
    stored: Stored | None = None
    content_type: str | None = None
    bytes: int = 0
    skipped: SkipReason | None = None
    error: str | None = None
    not_modified: bool = False  # a conditional request answered 304
    etag: str | None = None
    last_modified: str | None = None


def sha256_file(path: Path) -> str:
    """Hash a file without reading it into memory at once."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def new_tmp(root: Path) -> Path:
    """Return a fresh path for an unfinished download."""
    folder = root / TMP
    folder.mkdir(parents=True, exist_ok=True)
    return folder / uuid.uuid4().hex


def store(root: Path, url: str, tmp: Path, sha: str, run: str) -> Stored:
    """Move a finished download to <URL folder>/<sha256[:12]><suffix>, never overwriting.

    Content stored there before is not written again.
    """
    folder = resource_dir(url)
    relative = folder / f"{sha[:12]}{PurePosixPath(folder.name).suffix}"
    dest = root / relative
    if not dest.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"{url} would be stored outside {root}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if sha256_file(dest) != sha:
            raise FileExistsError(f"{dest} holds other content with the same short hash")
        tmp.unlink()
    else:
        tmp.replace(dest)
    return Stored(path=relative.as_posix(), sha256=sha, run=run)


def store_bytes(root: Path, url: str, data: bytes, run: str) -> Stored:
    """Store content already in memory, such as a page."""
    tmp = new_tmp(root)
    tmp.write_bytes(data)
    return store(root, url, tmp, hashlib.sha256(data).hexdigest(), run)


def download(
    client: httpx.Client, root: Path, url: str, run: str, etag: str | None = None
) -> Download:
    """Download one file, streaming and hashing it, and store it under its URL's key.

    With an etag it asks If-None-Match, and a 304 means unchanged. Video (by content type)
    and files over MAX_FILE_BYTES are skipped. Raises FirewallError if the firewall answers.
    """
    headers = {"If-None-Match": etag} if etag else {}

    def attempt() -> Download:
        with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
            if is_overloaded(response):
                raise FetchError(f"HTTP {response.status_code}")
            content_type = response.headers.get("content-type")
            length = int(response.headers.get("content-length") or 0)
            result = Download(url, content_type=content_type, bytes=length)
            if urlsplit(url).netloc == DATA_HOST:  # www's ETag and Last-Modified mean nothing
                result.etag = response.headers.get("etag")
                result.last_modified = response.headers.get("last-modified")
            if response.status_code == 304 and etag:
                result.not_modified = True
            elif response.status_code != 200:
                result.error = f"HTTP {response.status_code}"
            elif (content_type or "").startswith("video/"):
                result.skipped = "video"
            elif length > MAX_FILE_BYTES:
                result.skipped = "too_large"
            else:
                return finish_download(root, url, run, response, result)
            return result

    try:
        return with_retries(attempt, url)
    except FetchError as exc:
        return Download(url, error=str(exc))


def finish_download(
    root: Path, url: str, run: str, response: httpx.Response, result: Download
) -> Download:
    """Stream a response body to disk; stop at MAX_FILE_BYTES (www sends no length)."""
    tmp = new_tmp(root)
    digest = hashlib.sha256()
    result.bytes = 0
    with tmp.open("wb") as file:
        for block in response.iter_bytes():
            result.bytes += len(block)
            if result.bytes > MAX_FILE_BYTES:
                break
            digest.update(block)
            file.write(block)
    if result.bytes > MAX_FILE_BYTES:
        tmp.unlink()
        result.skipped = "too_large"
        return result
    if (result.content_type or "").startswith("text/html") and FIREWALL_MARKER in tmp.read_bytes():
        tmp.unlink()
        raise FirewallError(url)
    result.stored = store(root, url_key(url), tmp, digest.hexdigest(), run)
    return result


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
