import pytest
from conftest import (
    list_page,
    page,
)

from lawclerk.ingest.rulings import parse

pytestmark = pytest.mark.usefixtures("no_backoff")


@pytest.mark.parametrize(
    ("text", "size"),
    [
        ("Konsultationsdokument (pdf\xa0/ 220 KB)", 220_000),
        ("Anlage (pdf / 2,5 MB)", 2_500_000),
        ("Liste (xlsx / 1.024 KB)", 1_024_000),
        ("Änderungsbeschluss vom 18.01.2021", None),
    ],
)
def test_size_from_text(text: str, size: int | None) -> None:
    assert parse.size_from_text(text) == size


def test_main_sha256_ignores_the_menu() -> None:
    one = page("<p>Text</p>", nav='<a href="x">A</a>').encode()
    other = page("<p>Text</p>", nav='<a href="y">B</a>').encode()
    changed = page("<p>Neuer Text</p>").encode()
    assert parse.main_sha256(one) == parse.main_sha256(other) != parse.main_sha256(changed)
    assert parse.main_sha256(b"<html><body>no main</body></html>") is None


def test_squash_collapses_whitespace_and_non_breaking_spaces() -> None:
    assert parse.squash(" a\xa0 b\n c ") == "a b c"


def test_parse_page_reads_base_canonical_and_only_links_in_main() -> None:
    parsed = parse.parse_page(page('<a href="DE/a.html?nn=1">Seite&nbsp;A</a>').encode())
    assert parsed.base == "/"
    assert parsed.canonical == "http://x/y.html"
    assert parsed.has_main
    assert parsed.links == [("DE/a.html?nn=1", "Seite A")]


def test_parse_page_reads_list_rows_without_the_header() -> None:
    body = list_page(
        '<tr><td>BK6-18-004- RAM</td><td><p>Text <abbr title="Absatz">Abs.</abbr> 3</p>'
        '<a href="DE/x.html?nn=1">Beschluss</a></td><td>A GmbH</td><td></td></tr>'
    )
    (row,) = parse.parse_page(body.encode()).rows
    assert row.cells == ["BK6-18-004- RAM", "Text Abs. 3 Beschluss", "A GmbH", ""]
    assert row.links == [("DE/x.html?nn=1", "Beschluss")]


def test_parse_page_closes_a_link_opened_inside_another() -> None:
    parsed = parse.parse_page(
        page('<a href="https://x/a.html">A <a href="https://x/b.html">B</a>').encode()
    )
    assert parsed.links == [("https://x/a.html", "A"), ("https://x/b.html", "B")]


def test_parse_page_without_main() -> None:
    assert not parse.parse_page(b"<html><body><a href='x'>x</a></body></html>").has_main
