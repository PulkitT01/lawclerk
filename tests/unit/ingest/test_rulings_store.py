from pathlib import Path, PurePosixPath

import pytest
from conftest import (
    GZ,
    PDF,
    SITE,
)

from lawclerk.ingest.rulings import store

pytestmark = pytest.mark.usefixtures("no_backoff")


def test_resource_dir_decodes_spaces_and_umlauts() -> None:
    url = f"{SITE}/DE/BK6-22-011/Fl%C3%A4che%20N7.2_Bekanntmachung/Bekanntgabeseite.html"
    assert store.resource_dir(url) == PurePosixPath(
        "www.bundesnetzagentur.de",
        "DE",
        "BK6-22-011",
        "Fläche N7.2_Bekanntmachung",
        "Bekanntgabeseite.html",
    )


@pytest.mark.parametrize(
    "url",
    [
        f"{SITE}/DE/../../etc/passwd",
        f"{SITE}/DE/%2e%2e/x.pdf",
        f"{SITE}/DE/a%2Fb.pdf",
        f"{SITE}/DE/a%5Cb.pdf",
        f"{SITE}/DE//x.pdf",
        f"{SITE}/DE/.hidden",
        "https://www.acer.europa.eu/x.pdf",
    ],
)
def test_resource_dir_refuses_urls_that_could_leave_the_root(url: str) -> None:
    with pytest.raises(ValueError):
        store.resource_dir(url)


def test_store_never_overwrites_and_keeps_versions_side_by_side(tmp_path: Path) -> None:
    first = store.store_bytes(tmp_path, PDF, b"version 1", "r1")
    again = store.store_bytes(tmp_path, PDF, b"version 1", "r2")
    second = store.store_bytes(tmp_path, PDF, b"version 2", "r2")
    folder = tmp_path / "www.bundesnetzagentur.de" / GZ / "BK6-26-001_beschluss.pdf"
    assert first.path == again.path != second.path
    assert {path.name for path in folder.iterdir()} == {
        first.path.rsplit("/", 1)[1],
        second.path.rsplit("/", 1)[1],
    }
    assert first.path.endswith(".pdf")
    assert (tmp_path / first.path).read_bytes() == b"version 1"
    assert not list((tmp_path / store.TMP).iterdir())


def test_store_refuses_other_content_under_the_same_name(tmp_path: Path) -> None:
    stored = store.store_bytes(tmp_path, PDF, b"version 1", "r1")
    (tmp_path / stored.path).write_bytes(b"tampered")
    with pytest.raises(FileExistsError):
        store.store_bytes(tmp_path, PDF, b"version 1", "r2")
