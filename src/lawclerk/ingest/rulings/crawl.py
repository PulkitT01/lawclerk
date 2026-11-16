"""Crawl BK6: both lists and every page below the boundary, sorted into pages, files and skips."""

from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

from lawclerk.ingest.rulings.fetch import (
    HOSTS,
    MAX_CONCURRENT,
    SITE,
    FetchError,
    FirewallError,
    get,
    is_page_path,
    query_v,
    url_key,
)
from lawclerk.ingest.rulings.parse import PageParser, Row, parse_page
from lawclerk.ingest.rulings.snapshot import (
    BrokenRule,
    Link,
    SkipReason,
)
from lawclerk.ingest.rulings.store import is_safe

LISTS: Final = (
    f"https://{SITE}/DE/Beschlusskammern/BK06/BK6_21_AV/BK6_AV.html",  # closed proceedings
    f"https://{SITE}/DE/Beschlusskammern/BK06/BK6_11_LV/BK6_LV.html",  # ongoing proceedings
)
# The crawl follows every page below these: BK6's proceedings and BK6's topic pages
BOUNDARY: Final = ("/DE/Beschlusskammern/1_GZ/BK6-GZ/", "/DE/Beschlusskammern/BK06/")


MAX_PAGES: Final = 2000  # more pages than this means the crawl ran away: broken run


def in_boundary(path: str) -> bool:
    """Tell whether the crawl follows a page path: anywhere below BK6-GZ or BK06."""
    return path.startswith(BOUNDARY)


class BrokenRunError(Exception):
    """The crawl can't be trusted, so the run records nothing as gone."""

    def __init__(self, rule: BrokenRule, reason: str) -> None:
        super().__init__(reason)
        self.rule: BrokenRule = rule


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
