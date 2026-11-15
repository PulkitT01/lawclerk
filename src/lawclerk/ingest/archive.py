"""Read past versions of a law from QuantLaw's git archive of gesetze-im-internet.de.

The archive's ``data`` branch has one commit per daily scrape since 2019-06-10. It is kept
as a blobless clone: commits come down at once, file contents only when first read.
"""

import subprocess
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Final, NamedTuple

ARCHIVE_URL: Final = "https://github.com/QuantLaw/gesetze-im-internet.git"
WEB_URL: Final = "https://github.com/QuantLaw/gesetze-im-internet"
BRANCH: Final = "data"


class Snapshot(NamedTuple):
    """One archive commit that changed a law's XML file."""

    commit: str
    scraped: date


def git(repo: Path, *args: str) -> bytes:
    """Run a git command in repo and return its output."""
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def update(repo: Path, url: str = ARCHIVE_URL) -> None:
    """Clone the archive into repo without file contents, or fetch its new commits."""
    if repo.exists():
        git(repo, "fetch", "--quiet", "origin", f"+{BRANCH}:{BRANCH}")
        return
    repo.parent.mkdir(parents=True, exist_ok=True)
    git(
        repo.parent,
        "clone",
        "--quiet",
        "--bare",
        "--filter=blob:none",
        "--branch",
        BRANCH,
        url,
        repo.name,
    )


def newest_scrape(repo: Path) -> date:
    """Return the date of the archive's newest scrape, whatever law it changed."""
    return date.fromisoformat(git(repo, "log", "-1", "--format=%as", BRANCH).decode().strip())


def find_xml(repo: Path, slug: str) -> str:
    """Return the archive path of a law's XML file, e.g. data/items/enwg_2005/BJNR....xml."""
    names = git(repo, "ls-tree", "--name-only", BRANCH, f"data/items/{slug}/").decode().split()
    (xml,) = (name for name in names if name.endswith(".xml"))
    return xml


def snapshots(repo: Path, path: str) -> list[Snapshot]:
    """List the commits that changed path, oldest first.

    The scrape date is the author date: the 2019 commits were made later, so their
    committer date is wrong.
    """
    lines = git(repo, "log", "--reverse", "--format=%H %as", BRANCH, "--", path).decode()
    return [
        Snapshot(commit, date.fromisoformat(day))
        for commit, day in (line.split() for line in lines.splitlines())
    ]


def export(repo: Path, commit: str, folder: str, dest: Path) -> None:
    """Write every file in an archive folder, as it was at commit, into dest."""
    for line in git(repo, "ls-tree", commit, f"{folder}/").decode().splitlines():
        info, path = line.split("\t", 1)
        if info.split()[1] == "blob":  # skip subfolders
            (dest / PurePosixPath(path).name).write_bytes(
                git(repo, "cat-file", "blob", f"{commit}:{path}")
            )


def file_url(commit: str, path: str) -> str:
    """Return the web page of a file in the archive as it was at commit."""
    return f"{WEB_URL}/blob/{commit}/{path}"
