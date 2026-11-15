import subprocess
from datetime import date
from pathlib import Path

import pytest
from conftest import SourceArchive

from lawclerk.ingest import archive

XML = "data/items/testg/BJNR1.xml"


def test_update_clones_then_fetches_new_commits(
    source_archive: SourceArchive, tmp_path: Path
) -> None:
    source_archive.commit("2019-06-10", {XML: b"v1"})
    repo = tmp_path / "raw" / "archive.git"
    archive.update(repo, str(source_archive.path))
    assert archive.newest_scrape(repo) == date(2019, 6, 10)
    source_archive.commit("2019-06-11", {"data/items/other/x.xml": b"x"})
    archive.update(repo, str(source_archive.path))
    assert archive.newest_scrape(repo) == date(2019, 6, 11)


def test_update_raises_when_git_fails(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        archive.update(tmp_path / "archive.git", str(tmp_path / "missing"))


def test_find_xml_returns_the_laws_xml_file(source_archive: SourceArchive) -> None:
    source_archive.commit("2019-06-10", {XML: b"v1", "data/items/testg/f.jpg": b"img"})
    assert archive.find_xml(source_archive.path, "testg") == XML


def test_snapshots_lists_commits_that_changed_the_file_by_author_date(
    source_archive: SourceArchive,
) -> None:
    first = source_archive.commit("2019-06-10", {XML: b"v1"})
    source_archive.commit("2019-06-11", {"data/items/other/x.xml": b"x"})
    third = source_archive.commit("2020-01-02", {XML: b"v2"})
    assert archive.snapshots(source_archive.path, XML) == [
        archive.Snapshot(first, date(2019, 6, 10)),
        archive.Snapshot(third, date(2020, 1, 2)),
    ]


def test_export_writes_the_folder_as_it_was_at_a_commit(
    source_archive: SourceArchive, tmp_path: Path
) -> None:
    old = source_archive.commit(
        "2019-06-10", {XML: b"v1", "data/items/testg/f.jpg": b"img", "data/items/testg/sub/x": b""}
    )
    source_archive.commit("2019-06-11", {XML: b"v2"}, delete=("data/items/testg/f.jpg",))
    dest = tmp_path / "out"
    dest.mkdir()
    archive.export(source_archive.path, old, "data/items/testg", dest)
    assert sorted(p.name for p in dest.iterdir()) == ["BJNR1.xml", "f.jpg"]
    assert (dest / "BJNR1.xml").read_bytes() == b"v1"


def test_file_url_points_at_the_file_at_a_commit() -> None:
    assert archive.file_url("abc", XML) == (
        "https://github.com/QuantLaw/gesetze-im-internet/blob/abc/data/items/testg/BJNR1.xml"
    )
