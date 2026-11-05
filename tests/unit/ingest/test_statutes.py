import io
import json
import logging
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx
import pytest

from lawclerk.ingest import formulas, statutes


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
        law="TestG", title="Zweck", heading_path=[], status="in_force", version=None, **fields
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


def test_chunk_norm_turns_repealed_section_into_stub() -> None:
    norm = ET.fromstring(
        '<norm doknr="D-s2"><metadaten><enbez>§ 2</enbez><titel>(weggefallen)</titel></metadaten>'
        "<textdaten><text><Content><P/></Content></text></textdaten></norm>"
    )
    (chunk,) = statutes.chunk_norm(norm, law="TestG", slug="t", heading_path=[], version=None)
    assert (chunk.id, chunk.status, chunk.text) == ("D-s2", "repealed", "(weggefallen)")


def test_chunk_norm_skips_section_without_text(caplog: pytest.LogCaptureFixture) -> None:
    norm = ET.fromstring(
        '<norm doknr="D-x"><metadaten><enbez>Anlage</enbez><titel>(zu § 1)</titel></metadaten>'
        "<textdaten><text><Content><P/></Content></text></textdaten></norm>"
    )
    with caplog.at_level(logging.WARNING):
        chunks = statutes.chunk_norm(norm, law="TestG", slug="t", heading_path=[], version=None)
    assert chunks == []
    assert "no text" in caplog.text


def test_parse_statute_builds_chunks_with_heading_paths() -> None:
    chunks = statutes.parse_statute(ET.fromstring(LAW_XML), "TestG", "testg", {})
    assert [(c.section, c.heading_path, c.status) for c in chunks] == [
        ("§ 1", ["Teil 1 Allgemeines", "Abschnitt 1 Ziele"], "in_force"),
        ("§ 2", ["Teil 2 Schluss"], "repealed"),
        ("Anlage 1", [], "in_force"),
    ]
    assert chunks[0].version == "Zuletzt geändert durch X"
    assert chunks[0].source_url == "https://www.gesetze-im-internet.de/testg/__1.html"


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


def test_parse_statute_puts_formula_between_the_list_items_it_stood_in() -> None:
    xml = LAW_XML.replace(
        "<P>Tabelle</P>",
        "<P><DL><DT>1.</DT><DD><LA>Formel:</LA><LA>"
        '<IMG SRC="f.jpg" Type="jpg"/></LA></DD><DT>2.</DT><DD><LA>Ende</LA></DD></DL></P>',
    )
    chunks = statutes.parse_statute(ET.fromstring(xml), "TestG", "testg", {"f.jpg": "$$V_{it}=a$$"})
    assert chunks[-1].text == "1. Formel:\n$$V_{it}=a$$\n2. Ende"


def test_write_jsonl_writes_one_chunk_per_line(tmp_path: Path) -> None:
    chunks = [make_chunk(text="Übergang"), make_chunk(id="D-s2", text="zwei")]
    path = tmp_path / "out" / "law.jsonl"
    statutes.write_jsonl(chunks, path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [statutes.Chunk.model_validate_json(line) for line in lines] == chunks
    assert "Übergang" in lines[0]  # umlauts stored as-is, not escaped


@pytest.mark.parametrize(
    ("changed", "fetch_report"), [(True, "updated law.xml"), (False, "unchanged")]
)
def test_main_fetches_parses_and_writes_chunks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    changed: bool,
    fetch_report: str,
) -> None:
    def fake_fetch_law(client: httpx.Client, slug: str, output_dir: Path) -> list[Path]:
        path = output_dir / slug / "law.xml"
        path.parent.mkdir(parents=True)
        path.write_text(LAW_XML, encoding="utf-8")
        return [path] if changed else []

    monkeypatch.setattr(statutes, "LAWS", {"TestG": "testg"})
    monkeypatch.setattr(statutes, "OUTPUT_DIR", tmp_path / "raw")
    monkeypatch.setattr(statutes, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(statutes, "fetch_law", fake_fetch_law)
    statutes.main()
    assert capsys.readouterr().out == f"TestG: {fetch_report}\nTestG: wrote 3 chunks\n"
    lines = (tmp_path / "processed" / "testg.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["section"] for line in lines] == ["§ 1", "§ 2", "Anlage 1"]


def test_main_reads_formula_images_and_puts_them_into_the_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_fetch_law(client: httpx.Client, slug: str, output_dir: Path) -> list[Path]:
        folder = output_dir / slug
        folder.mkdir(parents=True)
        xml = LAW_XML.replace("<P>Tabelle</P>", '<P><IMG SRC="f.jpg"/></P>')
        (folder / "law.xml").write_text(xml, encoding="utf-8")
        (folder / "f.jpg").write_bytes(b"image bytes")
        return []

    monkeypatch.setattr(statutes, "LAWS", {"TestG": "testg"})
    monkeypatch.setattr(statutes, "OUTPUT_DIR", tmp_path / "raw")
    monkeypatch.setattr(statutes, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(statutes, "fetch_law", fake_fetch_law)
    monkeypatch.setattr(formulas, "ocr_formula", lambda image: f"$$latex of {image.name}$$")
    statutes.main()
    cache = formulas.load_cache(tmp_path / "processed" / statutes.FORMULAS_FILE)
    assert [entry.latex for entry in cache.values()] == ["$$latex of f.jpg$$"]
    lines = (tmp_path / "processed" / "testg.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["text"] == "$$latex of f.jpg$$"
