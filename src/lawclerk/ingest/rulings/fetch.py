"""Talk to the BNetzA site: its hosts, our User-Agent, retries, backoff and the firewall page."""

import time
from collections.abc import Callable
from typing import Final
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

SITE: Final = "www.bundesnetzagentur.de"
DATA_HOST: Final = "data.bundesnetzagentur.de"
HOSTS: Final = frozenset({SITE, DATA_HOST})


USER_AGENT: Final = "lawclerk/0.1 (+https://github.com/PulkitT01/lawclerk)"
FIREWALL_MARKER: Final = b"The requested URL was rejected"
MAX_CONCURRENT: Final = 4
MAX_RETRIES: Final = 3
BACKOFF_SECONDS: Final = 2.0  # doubled after each failed attempt
TIMEOUT_SECONDS: Final = 60.0


def url_key(url: str) -> str:
    """Return the key of a URL: https, host and path, without query or fragment."""
    parts = urlsplit(url)
    scheme = "https" if parts.netloc in HOSTS else parts.scheme
    return urlunsplit((scheme, parts.netloc, parts.path or "/", "", ""))


def query_v(url: str) -> str | None:
    """Return the v parameter of a URL, e.g. "3" for ...pdf?__blob=publicationFile&v=3."""
    values = parse_qs(urlsplit(url).query).get("v")
    return values[0] if values else None


def is_page_path(path: str) -> bool:
    """Tell whether a URL path names an HTML page: .html, or no file extension at all."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".html") or "." not in name


class FirewallError(Exception):
    """The site's firewall answered with its rejection page."""


class FetchError(Exception):
    """A request failed after every retry."""


def make_client() -> httpx.Client:
    """Return the HTTP client for the site: our own User-Agent, no automatic redirects."""
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)


def with_retries[T](attempt: Callable[[], T], url: str) -> T:
    """Run one request until it works, backing off after network errors, 429, 5xx and the
    firewall page. Raises FirewallError or FetchError after the last try."""
    error: Exception = FetchError(url)
    for number in range(MAX_RETRIES + 1):
        try:
            return attempt()
        except (httpx.TransportError, FetchError, FirewallError) as exc:
            error = exc
        if number < MAX_RETRIES:
            time.sleep(BACKOFF_SECONDS * 2**number)
    if isinstance(error, FirewallError):
        raise error
    raise FetchError(f"{url}: {error}") from error


def is_overloaded(response: httpx.Response) -> bool:
    """Tell whether a response asks us to come back later."""
    return response.status_code == 429 or response.status_code >= 500


def get(client: httpx.Client, url: str) -> httpx.Response:
    """GET a URL without following redirects, with retries."""

    def attempt() -> httpx.Response:
        response = client.get(url)
        if is_overloaded(response):
            raise FetchError(f"HTTP {response.status_code}")
        if FIREWALL_MARKER in response.content:
            raise FirewallError(url)
        return response

    return with_retries(attempt, url)


def server_v(client: httpx.Client, url: str) -> str | None:
    """Ask the site for a file's current v: its path without query redirects to ...&v=<n>.

    Returns None if the answer is no such redirect. Raises FirewallError or FetchError.
    """
    response = get(client, url)
    return query_v(response.headers.get("location", "")) if response.is_redirect else None
