"""The record of a run: snapshot lines, their format, and reading and writing snapshots."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

RUNS: Final = "runs"  # snapshot folder inside the root


FORMAT_VERSION: Final = 1


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
