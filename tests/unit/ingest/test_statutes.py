import io
import json
import logging
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest
from conftest import SourceArchive

from lawclerk.ingest import archive, formulas, statutes


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def mock_client(status: int, content: bytes, seen: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        return httpx.Response(status, content=content)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_zip_requests_slug_url() -> None:
    seen: list[str] = []
    with mock_client(200, b"zipbytes", seen) as client:
        assert statutes.download_zip(client, "enwg_2005") == b"zipbytes"
    assert seen == ["https://www.gesetze-im-internet.de/enwg_2005/xml.zip"]


def test_download_zip_raises_on_http_error() -> None:
    with mock_client(404, b"") as client, pytest.raises(httpx.HTTPStatusError):
        statutes.download_zip(client, "missing")


def test_save_if_changed_writes_new_file(tmp_path: Path) -> None:
    path = tmp_path / "law.xml"
    assert statutes.save_if_changed(path, b"v1") is True
    assert path.read_bytes() == b"v1"


def test_save_if_changed_skips_identical_file(tmp_path: Path) -> None:
    path = tmp_path / "law.xml"
    path.write_bytes(b"v1")
    assert statutes.save_if_changed(path, b"v1") is False


def test_save_if_changed_replaces_changed_file(tmp_path: Path) -> None:
    path = tmp_path / "law.xml"
    path.write_bytes(b"v1")
    assert statutes.save_if_changed(path, b"v2") is True
    assert path.read_bytes() == b"v2"


def test_extract_zip_writes_all_files(tmp_path: Path) -> None:
    archive = make_zip({"law.xml": b"<law/>", "image.jpg": b"jpg"})
    written = statutes.extract_zip(archive, tmp_path / "out")
    assert sorted(p.name for p in written) == ["image.jpg", "law.xml"]
    assert (tmp_path / "out" / "law.xml").read_bytes() == b"<law/>"
    assert (tmp_path / "out" / "image.jpg").read_bytes() == b"jpg"


def test_extract_zip_returns_only_changed_files(tmp_path: Path) -> None:
    statutes.extract_zip(make_zip({"law.xml": b"v1", "image.jpg": b"jpg"}), tmp_path)
    written = statutes.extract_zip(make_zip({"law.xml": b"v2", "image.jpg": b"jpg"}), tmp_path)
    assert written == [tmp_path / "law.xml"]


def test_extract_zip_deletes_files_no_longer_in_the_archive(tmp_path: Path) -> None:
    statutes.extract_zip(make_zip({"old.xml": b"v1", "image.jpg": b"jpg"}), tmp_path)
    written = statutes.extract_zip(make_zip({"new.xml": b"v2", "image.jpg": b"jpg"}), tmp_path)
    assert written == [tmp_path / "new.xml"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["image.jpg", "new.xml"]


def test_extract_zip_rejects_paths_outside_dest(tmp_path: Path) -> None:
    archive = make_zip({"../evil.xml": b"x"})
    with pytest.raises(ValueError, match="Unexpected path"):
        statutes.extract_zip(archive, tmp_path / "out")
    assert not (tmp_path / "evil.xml").exists()


def test_fetch_law_extracts_into_slug_folder(tmp_path: Path) -> None:
    with mock_client(200, make_zip({"law.xml": b"<law/>"})) as client:
        written = statutes.fetch_law(client, "enwg_2005", tmp_path)
    assert written == [tmp_path / "enwg_2005" / "law.xml"]


LAW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<dokumente doknr="D">
<norm doknr="D"><metadaten><jurabk>TestG</jurabk><standangabe><standtyp>Stand</standtyp>
<standkommentar>Zuletzt geändert durch X</standkommentar></standangabe></metadaten>
<textdaten><text><Content><P>Gesetz über Tests</P></Content></text></textdaten></norm>
<norm doknr="D-toc"><metadaten><enbez>Inhaltsübersicht</enbez></metadaten>
<textdaten><text><Content><P>§ 1 Zweck</P></Content></text></textdaten></norm>
<norm doknr="D-t1"><metadaten><gliederungseinheit><gliederungskennzahl>010</gliederungskennzahl>
<gliederungsbez>Teil 1</gliederungsbez><gliederungstitel>Allgemeines</gliederungstitel>
</gliederungseinheit></metadaten></norm>
<norm doknr="D-a1"><metadaten><gliederungseinheit><gliederungskennzahl>010010</gliederungskennzahl>
<gliederungsbez>Abschnitt 1</gliederungsbez><gliederungstitel>Ziele</gliederungstitel>
</gliederungseinheit></metadaten></norm>
<norm doknr="D-s1"><metadaten><enbez>§ 1</enbez><titel>Zweck</titel></metadaten>
<textdaten><text><Content><P>(1) Erster Absatz.</P></Content></text></textdaten></norm>
<norm doknr="D-t2"><metadaten><gliederungseinheit><gliederungskennzahl>020</gliederungskennzahl>
<gliederungsbez>Teil 2</gliederungsbez><gliederungstitel>Schluss</gliederungstitel>
</gliederungseinheit></metadaten></norm>
<norm doknr="D-s2"><metadaten><enbez>§ 2</enbez><titel>(weggefallen)</titel></metadaten>
<textdaten><text><Content><P/></Content></text></textdaten></norm>
<norm doknr="D-an"><metadaten><enbez>Anlage 1</enbez><titel>(zu § 1)</titel></metadaten>
<textdaten><text><Content><P>Tabelle</P></Content></text></textdaten></norm>
</dokumente>
"""


def make_chunk(**overrides: str) -> statutes.Chunk:
    fields = {"id": "D-s1", "section": "§ 1", "source_url": "u", "text": ""} | overrides
    return statutes.Chunk(
        law="TestG", title="Zweck", heading_path=[], status="in_force", stand_note=None, **fields
    )


def rendered(xml: str) -> str:
    return statutes.clean(statutes.render(ET.fromstring(xml)))


def test_clean_collapses_whitespace_and_drops_empty_lines() -> None:
    assert statutes.clean("  a   b \n\n   \n c ") == "a b\nc"


def test_render_glues_inline_markup_to_text() -> None:
    assert rendered("<P>Der <B>neue</B> Wert.</P>") == "Der neue Wert."


def test_render_marks_subscripts_and_superscripts_latex_style() -> None:
    xml = "<P>EO<SUB>t</SUB> = KA<SUB>dnb,t</SUB> · m<SUP>3</SUP></P>"
    assert rendered(xml) == "EO_{t} = KA_{dnb,t} · m^{3}"


def test_render_breaks_line_at_br() -> None:
    assert rendered("<P>eins<BR/>zwei</P>") == "eins\nzwei"


def test_render_puts_list_items_on_own_lines() -> None:
    xml = "<P>wenn<DL><DT>1.</DT><DD><LA>a</LA></DD><DT>2.</DT><DD><LA>b</LA></DD></DL>Ende</P>"
    assert rendered(xml) == "wenn\n1. a\n2. b\nEnde"


def test_render_table_rows_keep_empty_cells_in_place() -> None:
    xml = (
        "<P><table><tgroup><tbody><row><entry>Ebene</entry><entry>kW</entry><entry/>"
        "<entry>kWh</entry></row><row><entry/><entry>c</entry></row></tbody></tgroup></table></P>"
    )
    assert rendered(xml) == "Ebene | kW | | kWh\n| c"


def test_render_list_puts_term_and_definition_on_separate_lines() -> None:
    dl = ET.fromstring("<DL><DT>1.</DT><DD><LA>Begriff</LA><LA>Definition</LA></DD></DL>")
    assert statutes.clean(statutes.render_list(dl)) == "1. Begriff\nDefinition"


def test_render_list_keeps_subscripts_in_term() -> None:
    dl = ET.fromstring("<DL><DT>V<SUB>it</SUB></DT><DD><LA>die Vergütung</LA></DD></DL>")
    assert statutes.clean(statutes.render_list(dl)) == "V_{it} die Vergütung"


def test_split_nummern_keeps_subscripts_in_term() -> None:
    p = ET.fromstring("<P>Es ist<DL><DT>P<SUB>t</SUB></DT><DD>A</DD></DL></P>")
    assert statutes.split_nummern(p) == [("P_{t}", "Es ist\nP_{t} A")]


def test_split_nummern_repeats_intro_in_each_item() -> None:
    p = ET.fromstring("<P>Im Sinne ist<DL><DT>1.</DT><DD>A</DD><DT>2.</DT><DD>B</DD></DL></P>")
    assert statutes.split_nummern(p) == [("1", "Im Sinne ist\n1. A"), ("2", "Im Sinne ist\n2. B")]


def test_split_nummern_refuses_text_after_list() -> None:
    p = ET.fromstring("<P>Intro<DL><DT>1.</DT><DD>A</DD></DL>Schlusssatz</P>")
    assert statutes.split_nummern(p) is None


def test_split_section_keeps_short_section_whole() -> None:
    content = ET.fromstring("<Content><P>(1) a</P><P>(2) b</P></Content>")
    chunks = statutes.split_section(content, "(1) a\n(2) b", make_chunk())
    assert [(c.id, c.text) for c in chunks] == [("D-s1", "(1) a\n(2) b")]


def test_split_section_splits_long_section_by_absatz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(statutes, "MAX_CHUNK_CHARS", 20)
    content = ET.fromstring("<Content><P>(1) erster Absatz</P><P>(1a) eingefügt</P></Content>")
    text = statutes.clean(statutes.render(content))
    chunks = statutes.split_section(content, text, make_chunk())
    assert [(c.id, c.absatz, c.text) for c in chunks] == [
        ("D-s1-abs1", "1", "(1) erster Absatz"),
        ("D-s1-abs1a", "1a", "(1a) eingefügt"),
    ]


def test_split_section_splits_long_absatz_by_nummer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(statutes, "MAX_CHUNK_CHARS", 20)
    content = ET.fromstring(
        "<Content><P>Im Sinne dieses Gesetzes ist"
        "<DL><DT>1.</DT><DD><LA>Anlage</LA></DD><DT>2.</DT><DD><LA>Netz</LA></DD></DL>"
        "</P></Content>"
    )
    text = statutes.clean(statutes.render(content))
    chunks = statutes.split_section(content, text, make_chunk())
    assert [(c.id, c.absatz, c.nummer) for c in chunks] == [
        ("D-s1-nr1", None, "1"),
        ("D-s1-nr2", None, "2"),
    ]
    assert chunks[0].text == "Im Sinne dieses Gesetzes ist\n1. Anlage"


def test_split_section_keeps_oversized_absatz_without_list(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(statutes, "MAX_CHUNK_CHARS", 10)
    content = ET.fromstring("<Content><P>(1) ein sehr langer Absatz</P><P>(2) kurz</P></Content>")
    text = statutes.clean(statutes.render(content))
    with caplog.at_level(logging.WARNING):
        chunks = statutes.split_section(content, text, make_chunk())
    assert [c.id for c in chunks] == ["D-s1-abs1", "D-s1-abs2"]
    assert "no list to split on" in caplog.text


@pytest.mark.parametrize(
    "content_xml",
    [
        "<Content>Vorwort<P>(1) erster Absatz</P></Content>",
        "<Content><P>(1) erster Absatz</P>Nachsatz</Content>",
        "<Content><P>(1) erster Absatz</P><table><row><entry>Wert</entry></row></table></Content>",
    ],
)
def test_split_section_keeps_section_whole_when_text_is_outside_its_absaetze(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, content_xml: str
) -> None:
    monkeypatch.setattr(statutes, "MAX_CHUNK_CHARS", 10)
    content = ET.fromstring(content_xml)
    text = statutes.clean(statutes.render(content))
    with caplog.at_level(logging.WARNING):
        chunks = statutes.split_section(content, text, make_chunk())
    assert [(c.id, c.text) for c in chunks] == [("D-s1", text)]
    assert "text outside its Absätze" in caplog.text


def test_split_section_splits_by_absatz_across_a_line_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(statutes, "MAX_CHUNK_CHARS", 10)
    content = ET.fromstring("<Content><P>(1) erster</P><BR/><P>(2) zweiter</P></Content>")
    text = statutes.clean(statutes.render(content))
    chunks = statutes.split_section(content, text, make_chunk())
    assert [c.id for c in chunks] == ["D-s1-abs1", "D-s1-abs2"]


def test_split_section_skips_separator_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(statutes, "MAX_CHUNK_CHARS", 10)
    content = ET.fromstring("<Content><P>(1) erster</P><P><U>__________</U></P></Content>")
    text = statutes.clean(statutes.render(content))
    chunks = statutes.split_section(content, text, make_chunk())
    assert [c.id for c in chunks] == ["D-s1-abs1"]


@pytest.mark.parametrize(
    ("section", "page"),
    [("§ 23a", "__23a.html"), ("§ 3", "__3.html"), ("Anlage (zu § 13g)", "index.html")],
)
def test_section_url(section: str, page: str) -> None:
    assert statutes.section_url("enwg_2005", section) == (
        f"https://www.gesetze-im-internet.de/enwg_2005/{page}"
    )


def test_law_version_reads_stand_note() -> None:
    assert statutes.law_version(ET.fromstring(LAW_XML)) == "Zuletzt geändert durch X"


def test_law_version_is_none_without_stand_note() -> None:
    assert statutes.law_version(ET.fromstring("<dokumente><norm/></dokumente>")) is None


@pytest.mark.parametrize(
    ("title", "repealed"),
    [
        ("(weggefallen)", True),
        ("(aufgehoben)", True),
        ("Zweck (weggefallen)", False),
        (None, False),
    ],
)
def test_is_repealed(title: str | None, repealed: bool) -> None:
    assert statutes.is_repealed(title) is repealed


@pytest.mark.parametrize(
    ("norm_xml", "expected"),
    [
        (
            "<norm><metadaten><titel> Zweck </titel></metadaten><textdaten><text>"
            "<Content><P>(1) a</P><P>(2) b</P></Content></text></textdaten></norm>",
            ("Zweck", "(1) a\n(2) b"),
        ),
        (
            "<norm><metadaten><titel>(weggefallen)</titel></metadaten>"
            "<textdaten><text><Content><P/></Content></text></textdaten></norm>",
            ("(weggefallen)", "(weggefallen)"),
        ),
        ("<norm><metadaten/></norm>", (None, "")),
    ],
)
def test_section_text(norm_xml: str, expected: tuple[str | None, str]) -> None:
    assert statutes.section_text(ET.fromstring(norm_xml)) == expected


def test_chunk_norm_turns_repealed_section_into_stub() -> None:
    norm = ET.fromstring(
        '<norm doknr="D-s2"><metadaten><enbez>§ 2</enbez><titel>(weggefallen)</titel></metadaten>'
        "<textdaten><text><Content><P/></Content></text></textdaten></norm>"
    )
    (chunk,) = statutes.chunk_norm(norm, law="TestG", slug="t", heading_path=[], stand_note=None)
    assert (chunk.id, chunk.status, chunk.text) == ("D-s2", "repealed", "(weggefallen)")


def test_chunk_norm_skips_section_without_text(caplog: pytest.LogCaptureFixture) -> None:
    norm = ET.fromstring(
        '<norm doknr="D-x"><metadaten><enbez>Anlage</enbez><titel>(zu § 1)</titel></metadaten>'
        "<textdaten><text><Content><P/></Content></text></textdaten></norm>"
    )
    with caplog.at_level(logging.WARNING):
        chunks = statutes.chunk_norm(norm, law="TestG", slug="t", heading_path=[], stand_note=None)
    assert chunks == []
    assert "no text" in caplog.text


def test_read_sections_gives_each_section_its_heading_path_and_text() -> None:
    sections = statutes.read_sections(ET.fromstring(LAW_XML))
    assert [(name, s.heading_path, s.title, s.text) for name, s in sections.items()] == [
        ("§ 1", ["Teil 1 Allgemeines", "Abschnitt 1 Ziele"], "Zweck", "(1) Erster Absatz."),
        ("§ 2", ["Teil 2 Schluss"], "(weggefallen)", "(weggefallen)"),
        ("Anlage 1", [], "(zu § 1)", "Tabelle"),
    ]


def test_read_sections_leaves_out_section_without_text() -> None:
    xml = LAW_XML.replace("<P>Tabelle</P>", "<P/>")
    assert list(statutes.read_sections(ET.fromstring(xml))) == ["§ 1", "§ 2"]


def test_insert_formulas_puts_latex_where_the_image_stood() -> None:
    root = ET.fromstring('<P>vor <IMG SRC="a.jpg" Type="jpg"/> nach</P>')
    statutes.insert_formulas(root, {"a.jpg": "$$x_{i}$$"}, "TestG")
    assert statutes.clean(statutes.render(root)) == "vor $$x_{i}$$ nach"


def test_insert_formulas_leaves_image_without_formula_empty_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = ET.fromstring('<P>vor <IMG SRC="a.jpg"/><IMG/> nach</P>')
    with caplog.at_level(logging.WARNING):
        statutes.insert_formulas(root, {}, "TestG")
    assert statutes.clean(statutes.render(root)) == "vor nach"
    assert [r.getMessage() for r in caplog.records] == [
        "TestG: no formula for image 'a.jpg'",
        "TestG: no formula for image ''",
    ]


def test_formula_lands_between_the_list_items_it_stood_in() -> None:
    xml = LAW_XML.replace(
        "<P>Tabelle</P>",
        "<P><DL><DT>1.</DT><DD><LA>Formel:</LA><LA>"
        '<IMG SRC="f.jpg" Type="jpg"/></LA></DD><DT>2.</DT><DD><LA>Ende</LA></DD></DL></P>',
    )
    root = ET.fromstring(xml)
    statutes.insert_formulas(root, {"f.jpg": "$$V_{it}=a$$"}, "TestG")
    assert statutes.read_sections(root)["Anlage 1"].text == "1. Formel:\n$$V_{it}=a$$\n2. Ende"


def test_read_folder_puts_cached_formulas_into_the_xml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "law"
    folder.mkdir()
    (folder / "law.xml").write_text('<P>vor <IMG SRC="f.jpg"/> nach</P>', encoding="utf-8")
    (folder / "f.jpg").write_bytes(b"image bytes")
    monkeypatch.setattr(statutes, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(formulas, "ocr_formula", lambda image: f"$${image.name}$$")
    root = statutes.read_folder(folder, "TestG")
    assert statutes.clean(statutes.render(root)) == "vor $$f.jpg$$ nach"
    assert (tmp_path / "processed" / statutes.FORMULAS_FILE).exists()


def test_write_jsonl_writes_one_chunk_per_line(tmp_path: Path) -> None:
    chunks = [make_chunk(text="Übergang"), make_chunk(id="D-s2", text="zwei")]
    path = tmp_path / "out" / "law.jsonl"
    statutes.write_jsonl(chunks, path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [statutes.Chunk.model_validate_json(line) for line in lines] == chunks
    assert "Übergang" in lines[0]  # umlauts stored as-is, not escaped


def test_write_jsonl_refuses_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "law.jsonl"
    with pytest.raises(ValueError, match=r"Duplicate ids.*'D-s1'"):
        statutes.write_jsonl([make_chunk(), make_chunk(text="other")], path)
    assert not path.exists()


def test_split_section_adds_position_to_a_repeated_absatz_label(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(statutes, "MAX_CHUNK_CHARS", 10)
    content = ET.fromstring("<Content><P>(1f) erster</P><P>(2) b</P><P>(1f) zweiter</P></Content>")
    text = statutes.clean(statutes.render(content))
    with caplog.at_level(logging.WARNING):
        chunks = statutes.split_section(content, text, make_chunk())
    assert [(c.id, c.absatz) for c in chunks] == [
        ("D-s1-abs1f", "1f"),
        ("D-s1-abs2", "2"),
        ("D-s1-abs1f-p3", "1f"),
    ]
    assert "Absatz (1f) appears twice" in caplog.text


# Each snapshot: section name -> (doknr, title, text)
SnapshotSections = dict[str, tuple[str, str, str]]


def snapshot_xml(sections: SnapshotSections, stand: str = "Stand A") -> ET.Element:
    norms = "".join(
        f'<norm doknr="{doknr}"><metadaten><enbez>{name}</enbez><titel>{title}</titel>'
        f"</metadaten><textdaten><text><Content><P>{text}</P></Content></text></textdaten></norm>"
        for name, (doknr, title, text) in sections.items()
    )
    return ET.fromstring(
        '<dokumente><norm doknr="D"><metadaten><standangabe><standtyp>Stand</standtyp>'
        f"<standkommentar>{stand}</standkommentar></standangabe></metadaten></norm>"
        f"{norms}</dokumente>"
    )


def build(*snapshots: tuple[str, ET.Element]) -> statutes.History:
    """Build a history from (scrape date, root) pairs; the commit of snapshot i is "c<i>"."""
    return statutes.build_history(
        ((f"c{i}", date.fromisoformat(day), root) for i, (day, root) in enumerate(snapshots)),
        law="TestG",
        slug="testg",
        xml_file="data/items/testg/L.xml",
    )


def spans(history: statutes.History) -> list[tuple[str, str, str | None, str | None, str]]:
    """(version id, text, valid_from, valid_to, change) of every version."""

    def day(value: date | None) -> str | None:
        return str(value) if value else None

    return [(v.id, v.text, day(v.valid_from), day(v.valid_to), v.change) for v in history.versions]


S1 = {"§ 1": ("D-1", "Zweck", "eins")}


def test_build_history_versions_in_the_first_snapshot_have_unknown_start() -> None:
    history = build(("2019-06-10", snapshot_xml(S1 | {"§ 2": ("D-2", "Ziel", "zwei")})))
    assert spans(history) == [
        ("TestG § 1@2019-06-10", "eins", None, None, "initial"),
        ("TestG § 2@2019-06-10", "zwei", None, None, "initial"),
    ]
    version = history.versions[0]
    assert (version.first_seen, version.commit) == (date(2019, 6, 10), "c0")
    assert (version.doknr, version.title, version.status) == ("D-1", "Zweck", "in_force")
    assert version.stand_note == "Stand A"
    assert version.source_url == "https://www.gesetze-im-internet.de/testg/__1.html"


def test_build_history_marks_a_repealed_section_as_repealed() -> None:
    history = build(("2019-06-10", snapshot_xml({"§ 2": ("D-2", "(weggefallen)", "")})))
    (version,) = history.versions
    assert (version.status, version.text) == ("repealed", "(weggefallen)")
    assert [(c.status, c.text) for c in history.chunks] == [("repealed", "(weggefallen)")]


def test_build_history_snapshot_without_text_change_adds_no_version() -> None:
    history = build(
        ("2019-06-10", snapshot_xml(S1)),
        ("2019-06-11", snapshot_xml(S1, stand="Stand B")),
    )
    assert spans(history) == [("TestG § 1@2019-06-10", "eins", None, None, "initial")]


def test_build_history_new_doknr_is_an_amendment_and_ends_the_old_version() -> None:
    history = build(
        ("2019-06-10", snapshot_xml(S1)),
        ("2020-11-06", snapshot_xml({"§ 1": ("D-1b", "Zweck", "neu")}, stand="Stand B")),
    )
    assert spans(history) == [
        ("TestG § 1@2019-06-10", "eins", None, "2020-11-06", "initial"),
        ("TestG § 1@2020-11-06", "neu", "2020-11-06", None, "amendment"),
    ]
    old, new = history.versions
    assert old.source_url == (
        "https://github.com/QuantLaw/gesetze-im-internet/blob/c0/data/items/testg/L.xml"
    )
    assert new.source_url == "https://www.gesetze-im-internet.de/testg/__1.html"
    assert (new.stand_note, new.commit) == ("Stand B", "c1")


def test_build_history_same_doknr_with_new_text_is_a_correction() -> None:
    history = build(
        ("2019-06-10", snapshot_xml(S1)),
        ("2019-06-11", snapshot_xml({"§ 1": ("D-1", "Zweck", "eins, berichtigt")})),
    )
    assert spans(history)[1][-1] == "correction"


def test_build_history_new_title_with_same_text_starts_a_version() -> None:
    history = build(
        ("2019-06-10", snapshot_xml(S1)),
        ("2019-06-11", snapshot_xml({"§ 1": ("D-1", "Neuer Titel", "eins")})),
    )
    assert [v.title for v in history.versions] == ["Zweck", "Neuer Titel"]


def test_build_history_absence_ends_a_version_and_a_return_starts_a_new_one() -> None:
    # Enough other sections that losing § 1 doesn't look like a broken snapshot
    other = {f"§ {n}": (f"D-{n}", "T", f"text {n}") for n in range(2, 12)}
    history = build(
        ("2019-06-10", snapshot_xml(S1 | other)),
        ("2019-06-11", snapshot_xml(other)),
        ("2019-06-12", snapshot_xml(S1 | other)),
    )
    assert [span for span in spans(history) if span[0].startswith("TestG § 1@")] == [
        ("TestG § 1@2019-06-10", "eins", None, "2019-06-11", "initial"),
        ("TestG § 1@2019-06-12", "eins", "2019-06-12", None, "initial"),
    ]
    assert len(history.versions) == 12


def test_build_history_text_that_returns_is_a_new_version() -> None:
    history = build(
        ("2019-06-10", snapshot_xml(S1)),
        ("2019-06-11", snapshot_xml({"§ 1": ("D-1b", "Zweck", "B")})),
        ("2019-06-12", snapshot_xml(S1)),
    )
    assert [(v.text, v.change) for v in history.versions] == [
        ("eins", "initial"),
        ("B", "amendment"),
        ("eins", "amendment"),
    ]


def test_build_history_skips_snapshot_that_lost_sections(
    caplog: pytest.LogCaptureFixture,
) -> None:
    full = {f"§ {n}": (f"D-{n}", "T", f"text {n}") for n in range(1, 11)}
    broken = {name: value for name, value in full.items() if name < "§ 5"}
    with caplog.at_level(logging.WARNING):
        history = build(
            ("2026-04-25", snapshot_xml(full)),
            ("2026-05-05", snapshot_xml(broken)),
            ("2026-05-07", snapshot_xml(full)),
        )
    assert history.skipped == [(date(2026, 5, 5), len(broken), 10)]
    assert [v.valid_to for v in history.versions] == [None] * 10
    assert list(history.newest) == list(full)
    assert "skipped snapshot 2026-05-05" in caplog.text


def test_build_history_chunks_each_version_and_links_it() -> None:
    history = build(
        ("2019-06-10", snapshot_xml(S1)),
        ("2020-11-06", snapshot_xml({"§ 1": ("D-1b", "Zweck", "neu")}, stand="Stand B")),
    )
    assert [(c.id, c.version_id, c.text, c.stand_note) for c in history.chunks] == [
        ("D-1@2019-06-10", "TestG § 1@2019-06-10", "eins", "Stand A"),
        ("D-1b@2020-11-06", "TestG § 1@2020-11-06", "neu", "Stand B"),
    ]
    assert [c.source_url for c in history.chunks] == [v.source_url for v in history.versions]


def test_version_at_uses_exclusive_end_and_nothing_before_the_archive() -> None:
    history = build(
        ("2019-06-10", snapshot_xml(S1)),
        ("2026-07-29", snapshot_xml({"§ 1": ("D-1b", "Zweck", "neu")})),
    )
    versions = history.versions

    def text_on(day: str) -> str | None:
        version = statutes.version_at(versions, "§ 1", date.fromisoformat(day))
        return version.text if version else None

    assert text_on("2019-06-09") is None
    assert text_on("2019-06-10") == "eins"
    assert text_on("2026-07-28") == "eins"
    assert text_on("2026-07-29") == "neu"
    assert statutes.version_at(versions, "§ 9", date(2026, 8, 1)) is None


def test_archive_snapshots_reads_each_commit_with_its_own_images(
    source_archive: SourceArchive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    xml = '<dokumente><norm doknr="D"><metadaten><enbez>§ 1</enbez></metadaten><textdaten>'
    xml += (
        '<text><Content><P><IMG SRC="f.jpg"/></P></Content></text></textdaten></norm></dokumente>'
    )
    law = {"data/items/testg/L.xml": xml.encode()}
    first = source_archive.commit("2019-06-10", law | {"data/items/testg/f.jpg": b"old image"})
    rebuilt = {
        "data/items/testg/L.xml": xml.replace('doknr="D"', 'doknr="D" builddate="2"').encode()
    }
    second = source_archive.commit("2020-08-20", rebuilt | {"data/items/testg/f.jpg": b"new image"})
    monkeypatch.setattr(statutes, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(formulas, "ocr_formula", lambda image: image.read_text())
    snapshots = archive.snapshots(source_archive.path, "data/items/testg/L.xml")
    read = statutes.archive_snapshots(source_archive.path, snapshots, "data/items/testg/L.xml", "T")
    assert [
        (commit, day, statutes.read_sections(root)["§ 1"].text) for commit, day, root in read
    ] == [
        (first, date(2019, 6, 10), "old image"),
        (second, date(2020, 8, 20), "new image"),
    ]


def test_changed_sections_lists_changed_added_and_removed_sections() -> None:
    old = statutes.read_sections(
        snapshot_xml(S1 | {"§ 2": ("D-2", "T", "a"), "§ 3": ("D-3", "T", "c")})
    )
    new = statutes.read_sections(
        snapshot_xml(S1 | {"§ 2": ("D-x", "T", "b"), "§ 4": ("D-4", "T", "d")})
    )
    assert statutes.changed_sections(old, new) == ["§ 2", "§ 3", "§ 4"]
    assert statutes.changed_sections(old, old) == []


ARCHIVE_XML = "data/items/testg/BJNR1.xml"
AMENDED_XML = LAW_XML.replace("(1) Erster Absatz.", "(1) Neuer Absatz.")


@pytest.fixture
def main_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source_archive: SourceArchive
) -> Callable[[str], None]:
    """Point main at tmp_path and the local archive; return a setter for the live XML."""
    live = {"xml": LAW_XML}

    def fake_fetch_law(client: httpx.Client, slug: str, output_dir: Path) -> list[Path]:
        path = output_dir / slug / "law.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(live["xml"], encoding="utf-8")
        return [path]

    real_update = archive.update
    monkeypatch.setattr(archive, "update", lambda repo: real_update(repo, str(source_archive.path)))
    monkeypatch.setattr(statutes, "LAWS", {"TestG": "testg"})
    monkeypatch.setattr(statutes, "OUTPUT_DIR", tmp_path / "raw")
    monkeypatch.setattr(statutes, "ARCHIVE_DIR", tmp_path / "raw" / "archive.git")
    monkeypatch.setattr(statutes, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(statutes, "fetch_law", fake_fetch_law)
    return lambda xml: live.update(xml=xml)


def test_main_builds_history_from_the_archive_and_writes_versions_and_chunks(
    main_setup: Callable[[str], None],
    source_archive: SourceArchive,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    today = str(date.today())
    source_archive.commit("2019-06-10", {ARCHIVE_XML: LAW_XML.encode()})
    source_archive.commit(today, {ARCHIVE_XML: AMENDED_XML.encode()})
    main_setup(AMENDED_XML)
    with caplog.at_level(logging.WARNING):
        statutes.main()
    assert capsys.readouterr().out == (
        f"TestG: newest archive scrape {today}, 2 snapshots\n"
        "TestG: 4 versions of 3 sections, 3 open\n"
        "TestG: wrote 4 chunks\n"
        "TestG: live text matches the newest snapshot\n"
    )
    assert caplog.text == ""
    processed = tmp_path / "processed"
    versions = (processed / "testg.versions.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in versions] == [
        "TestG § 1@2019-06-10",
        "TestG § 2@2019-06-10",
        "TestG Anlage 1@2019-06-10",
        f"TestG § 1@{today}",
    ]
    chunks = (processed / "testg.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in chunks] == [
        "D-s1@2019-06-10",
        "D-s2@2019-06-10",
        "D-an@2019-06-10",
        f"D-s1@{today}",
    ]


def test_main_warns_about_a_stale_archive_and_changed_live_text(
    main_setup: Callable[[str], None],
    source_archive: SourceArchive,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_archive.commit("2019-06-10", {ARCHIVE_XML: LAW_XML.encode()})
    main_setup(AMENDED_XML)
    with caplog.at_level(logging.WARNING):
        statutes.main()
    assert "The archive's newest scrape is from 2019-06-10" in caplog.text
    assert "TestG: live text differs from the newest snapshot in § 1" in caplog.text
    assert "live text matches" not in capsys.readouterr().out


def test_main_reads_formula_images_and_puts_them_into_the_chunks(
    main_setup: Callable[[str], None],
    source_archive: SourceArchive,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    xml = LAW_XML.replace("<P>Tabelle</P>", '<P><IMG SRC="f.jpg"/></P>')
    source_archive.commit(
        "2019-06-10", {ARCHIVE_XML: xml.encode(), "data/items/testg/f.jpg": b"image bytes"}
    )
    monkeypatch.setattr(formulas, "ocr_formula", lambda image: f"$$latex of {image.name}$$")
    statutes.main()
    cache = formulas.load_cache(tmp_path / "processed" / statutes.FORMULAS_FILE)
    assert [entry.latex for entry in cache.values()] == ["$$latex of f.jpg$$"]
    lines = (tmp_path / "processed" / "testg.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["text"] == "$$latex of f.jpg$$"
