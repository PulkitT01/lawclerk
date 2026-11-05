import logging
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
    for name in ["a.jpg", "b.jpg", "c.jpg"]:
        (tmp_path / name).write_bytes(b"image")
    assert formulas.find_images(ET.fromstring(XML), tmp_path) == [
        tmp_path / "a.jpg",
        tmp_path / "b.jpg",
        tmp_path / "c.jpg",  # the ../outside/ part is dropped
    ]


def test_find_images_is_empty_without_images(tmp_path: Path) -> None:
    root = ET.fromstring("<dokumente><P>Text</P></dokumente>")
    assert formulas.find_images(root, tmp_path) == []


def test_find_images_leaves_out_and_logs_missing_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "a.jpg").write_bytes(b"image")
    root = ET.fromstring('<P><IMG SRC="a.jpg"/><IMG SRC="missing.jpg"/></P>')
    with caplog.at_level(logging.WARNING):
        assert formulas.find_images(root, tmp_path) == [tmp_path / "a.jpg"]
    assert "'missing.jpg' is missing" in caplog.text


def test_normalize_latex_drops_font_commands_and_keeps_delimiters() -> None:
    raw = r" $$ \mathsf{V}_{\mathrm{it}}=\frac{\mathsf{C}_{\mathrm{i}}}{\mathsf{E}}*\mathsf{EUA} $$"
    assert formulas.normalize_latex(raw) == r"$$ V_{it}=\frac{C_{i}}{E}*EUA $$"


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
    assert formulas.ocr_formula(Path("x.jpg")) == "$$ V_{it} $$"
    assert pipeline.calls == [("x.jpg", "formula")]


@pytest.fixture
def fresh_pipeline() -> Iterator[None]:
    """Empty load_pipeline's cache before and after, so no fake model leaks into other tests."""
    formulas.load_pipeline.cache_clear()
    yield
    formulas.load_pipeline.cache_clear()


@pytest.mark.usefixtures("fresh_pipeline")
def test_load_pipeline_loads_the_model_once_without_layout_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, Any]] = []

    class FakePaddleOCRVL:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    paddleocr = ModuleType("paddleocr")
    monkeypatch.setattr(paddleocr, "PaddleOCRVL", FakePaddleOCRVL, raising=False)
    monkeypatch.setitem(sys.modules, "paddleocr", paddleocr)
    assert formulas.load_pipeline() is formulas.load_pipeline()
    assert created == [{"use_layout_detection": False}]


def test_load_cache_is_empty_when_file_is_missing(tmp_path: Path) -> None:
    assert formulas.load_cache(tmp_path / "formulas.json") == {}


def test_cache_round_trips_through_file(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "formulas.json"
    cache = {"abc": formulas.FormulaEntry(file="a.jpg", latex="x_{i}", model="m")}
    formulas.save_cache(cache, path)
    assert formulas.load_cache(path) == cache


def test_update_cache_reads_new_images_and_stores_them(tmp_path: Path) -> None:
    image = tmp_path / "a.jpg"
    image.write_bytes(b"image bytes")
    cache_path = tmp_path / "formulas.json"
    assert formulas.update_cache([image], cache_path, FakeOcr()) == {"a.jpg": "latex of a.jpg"}
    assert formulas.load_cache(cache_path) == {
        formulas.image_key(image): formulas.FormulaEntry(
            file="a.jpg", latex="latex of a.jpg", model=formulas.MODEL_NAME
        )
    }


def test_update_cache_saves_each_image_before_reading_the_next(tmp_path: Path) -> None:
    first, broken = tmp_path / "a.jpg", tmp_path / "b.jpg"
    first.write_bytes(b"first")
    broken.write_bytes(b"broken")
    cache_path = tmp_path / "formulas.json"

    def ocr(image: Path) -> str:
        if image == broken:
            raise RuntimeError("model failed")
        return "x"

    with pytest.raises(RuntimeError):
        formulas.update_cache([first, broken], cache_path, ocr)
    assert [entry.file for entry in formulas.load_cache(cache_path).values()] == ["a.jpg"]


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
    corrected = {
        formulas.image_key(image): formulas.FormulaEntry(file="a.jpg", latex="fixed", model="me")
    }
    formulas.save_cache(corrected, cache_path)
    assert formulas.update_cache([image], cache_path, FakeOcr()) == {"a.jpg": "fixed"}
    assert formulas.load_cache(cache_path) == corrected


def test_update_cache_does_not_write_a_file_when_there_is_nothing_to_read(tmp_path: Path) -> None:
    cache_path = tmp_path / "formulas.json"
    assert formulas.update_cache([], cache_path, FakeOcr()) == {}
    assert not cache_path.exists()
