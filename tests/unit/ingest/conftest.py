import os
import subprocess
from pathlib import Path

import pytest


class SourceArchive:
    """A local git repository shaped like QuantLaw's archive, to clone instead of GitHub."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.run("init", "--quiet", "--initial-branch=data")

    def run(self, *args: str, env: dict[str, str] | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
            text=True,
            env=os.environ | (env or {}),
        ).stdout

    def commit(self, day: str, files: dict[str, bytes], delete: tuple[str, ...] = ()) -> str:
        """Write files (paths relative to the repo), delete others, commit as scraped on day."""
        for name, data in files.items():
            path = self.path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for name in delete:
            (self.path / name).unlink()
        self.run("add", "--all")
        stamp = f"{day}T04:00:00"
        env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": "2020-05-02T00:00:00"}
        self.run("commit", "--quiet", "--allow-empty", "-m", f"scrape {stamp}", env=env)
        return self.run("rev-parse", "HEAD").strip()


@pytest.fixture
def source_archive(tmp_path: Path) -> SourceArchive:
    path = tmp_path / "source"
    path.mkdir()
    return SourceArchive(path)
