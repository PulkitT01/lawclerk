from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lawclerk.ingest import formulas

XML = (
    "<dokumente><norm><Content><P>"
    '<IMG SRC="a.jpg" Type="jpg"/><IMG SRC="b.jpg"/><IMG SRC="a.jpg"/><IMG/>'
    '<IMG SRC="../outside/c.jpg"/>'
    "</P></Content></norm></dokumente>"
)


class FakePipeline:
    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.calls: list[tuple[str, str]] = []

    def predict(self, image: str, *, prompt_label: str) -> list[Any]:
        self.calls.append((image, prompt_label))
        return [SimpleNamespace(markdown={"markdown_texts": self.markdown})]


class FakeOcr:
    """Stands in for ocr_formula and remembers which images it was asked to read."""

    def __init__(self) -> None:
        self.images: list[str] = []

    def __call__(self, image: Path) -> str:
        self.images.append(image.name)
        return f"latex of {image.name}"


def test_find_images_returns_each_file_once_from_the_xml_folder(tmp_path: Path) -> None:
    xml_path = tmp_path / "law.xml"
    xml_path.write_text(XML, encoding="utf-8")
    assert formulas.find_images(xml_path) == [
        tmp_path / "a.jpg",
        tmp_path / "b.jpg",
        tmp_path / "c.jpg",  # the ../outside/ part is dropped
    ]


def test_find_images_is_empty_without_images(tmp_path: Path) -> None:
    xml_path = tmp_path / "law.xml"
    xml_path.write_text("<dokumente><P>Text</P></dokumente>", encoding="utf-8")
    assert formulas.find_images(xml_path) == []


def test_normalize_latex_drops_delimiters_and_font_commands() -> None:
    raw = r" $$ \mathsf{V}_{\mathrm{it}}=\frac{\mathsf{C}_{\mathrm{i}}}{\mathsf{E}}*\mathsf{EUA} $$"
    assert formulas.normalize_latex(raw) == r"V_{it}=\frac{C_{i}}{E}*EUA"


def test_normalize_latex_handles_nested_font_commands() -> None:
    assert formulas.normalize_latex(r"\mathsf{\mathrm{x}}+y") == "x+y"


def test_normalize_latex_keeps_other_commands() -> None:
    raw = r"\left[a\right]\mathbf{b}"
    assert formulas.normalize_latex(raw) == raw


def test_ocr_formula_asks_for_a_formula_and_normalizes_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline(r"$$ \mathsf{V}_{\mathrm{it}} $$")
    monkeypatch.setattr(formulas, "load_pipeline", lambda: pipeline)
    assert formulas.ocr_formula(Path("x.jpg")) == "V_{it}"
    assert pipeline.calls == [("x.jpg", "formula")]


def test_load_cache_is_empty_when_file_is_missing(tmp_path: Path) -> None:
    assert formulas.load_cache(tmp_path / "formulas.json") == {}


def test_cache_round_trips_through_file(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "formulas.json"
    cache = {"abc": formulas.FormulaEntry(file="a.jpg", latex="x_{i}", model="m")}
    formulas.save_cache(cache, path)
    assert formulas.load_cache(path) == cache


def test_latex_by_file_maps_names_to_latex_and_skips_uncached_images(tmp_path: Path) -> None:
    known, unknown = tmp_path / "a.jpg", tmp_path / "b.jpg"
    known.write_bytes(b"known")
    unknown.write_bytes(b"unknown")
    entry = formulas.FormulaEntry(file="a.jpg", latex="x_{i}", model="m")
    cache = {formulas.image_key(known): entry}
    assert formulas.latex_by_file([known, unknown], cache) == {"a.jpg": "x_{i}"}


def test_update_cache_reads_new_images_and_stores_them(tmp_path: Path) -> None:
    image = tmp_path / "a.jpg"
    image.write_bytes(b"image bytes")
    cache_path = tmp_path / "formulas.json"
    ocr = FakeOcr()
    cache = formulas.update_cache([image], cache_path, ocr)
    # SHA-1 of b"image bytes"
    (key,) = cache
    assert len(key) == 40
    assert cache[key] == formulas.FormulaEntry(
        file="a.jpg", latex="latex of a.jpg", model=formulas.MODEL_NAME
    )
    assert formulas.load_cache(cache_path) == cache


def test_update_cache_skips_images_already_in_the_cache(tmp_path: Path) -> None:
    image = tmp_path / "a.jpg"
    image.write_bytes(b"image bytes")
    cache_path = tmp_path / "formulas.json"
    formulas.update_cache([image], cache_path, FakeOcr())
    ocr = FakeOcr()
    formulas.update_cache([image], cache_path, ocr)
    assert ocr.images == []


def test_update_cache_keeps_hand_corrected_entries(tmp_path: Path) -> None:
    image = tmp_path / "a.jpg"
    image.write_bytes(b"image bytes")
    cache_path = tmp_path / "formulas.json"
    (key,) = formulas.update_cache([image], cache_path, FakeOcr())
    corrected = {key: formulas.FormulaEntry(file="a.jpg", latex="fixed", model="by hand")}
    formulas.save_cache(corrected, cache_path)
    assert formulas.update_cache([image], cache_path, FakeOcr()) == corrected


def test_update_cache_does_not_write_a_file_when_there_is_nothing_to_read(tmp_path: Path) -> None:
    cache_path = tmp_path / "formulas.json"
    assert formulas.update_cache([], cache_path, FakeOcr()) == {}
    assert not cache_path.exists()
