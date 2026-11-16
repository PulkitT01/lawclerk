"""Store what was downloaded: one folder per URL, one file per version, never overwritten."""

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import unquote, urlsplit

import httpx

from lawclerk.ingest.rulings.fetch import (
    DATA_HOST,
    FIREWALL_MARKER,
    HOSTS,
    FetchError,
    FirewallError,
    is_overloaded,
    url_key,
    with_retries,
)
from lawclerk.ingest.rulings.snapshot import (
    SkipReason,
    Stored,
)

TMP: Final = ".tmp"  # unfinished downloads inside the root


MAX_FILE_BYTES: Final = 100_000_000


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
