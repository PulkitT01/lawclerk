"""OCR the formula images in statute XML files into LaTeX, cached by image hash.

PaddleOCR is in the optional ``ocr`` extra (``uv sync --extra ocr``) and is only imported
when an image has to be read, so the rest of the ingest code (and its tests) never loads it.
"""

import functools
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, TypeAdapter

logger = logging.getLogger(__name__)

MODEL_NAME: Final = "PaddleOCR-VL-1.6"

# The model wraps every letter in a font command: \mathsf{V}_{\mathrm{it}} -> V_{it}
FONT_COMMAND: Final = re.compile(r"\\math(?:sf|rm)\{([^{}]*)\}")


class FormulaEntry(BaseModel):
    """The LaTeX read from one image, plus where it came from."""

    file: str
    latex: str
    model: str


_CACHE_FORMAT: Final = TypeAdapter(dict[str, FormulaEntry])


def find_images(root: ET.Element, folder: Path) -> list[Path]:
    """Return the image files a statute XML refers to, each once, found in its folder.

    An image that isn't in the folder is left out and logged.
    """
    # Path(...).name: only ever read files from the XML's own folder
    names = [Path(src).name for img in root.iter("IMG") if (src := img.get("SRC"))]
    images = []
    for name in dict.fromkeys(names):
        image = folder / name
        if image.is_file():
            images.append(image)
        else:
            logger.warning("Image %r is missing from %s", name, folder)
    return images


def normalize_latex(raw: str) -> str:
    """Drop the font commands the model wraps around every letter; keep its delimiters."""
    latex = raw.strip()
    while (stripped := FONT_COMMAND.sub(r"\1", latex)) != latex:  # repeat for nested commands
        latex = stripped
    return latex


@functools.cache
def load_pipeline() -> Any:
    """Load the PaddleOCR-VL model once (about 1.8 GB, downloaded on first use)."""
    from paddleocr import PaddleOCRVL

    # The images are already cropped to the formula, so skip finding it on a page
    return PaddleOCRVL(use_layout_detection=False)


def ocr_formula(image: Path) -> str:
    """Read the formula in an image and return it as LaTeX."""
    (result,) = load_pipeline().predict(str(image), prompt_label="formula")
    return normalize_latex(result.markdown["markdown_texts"])


def load_cache(path: Path) -> dict[str, FormulaEntry]:
    """Read the cache (image SHA-1 -> entry); an empty one if the file doesn't exist yet."""
    if not path.exists():
        return {}
    return _CACHE_FORMAT.validate_json(path.read_bytes())


def save_cache(cache: dict[str, FormulaEntry], path: Path) -> None:
    """Write the cache as JSON, readable and editable by hand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_CACHE_FORMAT.dump_json(cache, indent=2))


def image_key(image: Path) -> str:
    """Return the cache key of an image: the SHA-1 of its bytes."""
    return hashlib.sha1(image.read_bytes()).hexdigest()


def update_cache(
    images: list[Path], cache_path: Path, ocr: Callable[[Path], str]
) -> dict[str, str]:
    """OCR the images that are not in the cache yet; map each image's file name to its LaTeX.

    The cache is saved after every image read, so a failure keeps the ones read before it.
    """
    cache = load_cache(cache_path)
    latex_by_file = {}
    for image in images:
        key = image_key(image)
        if key not in cache:
            logger.info("Reading formula in %s", image.name)
            cache[key] = FormulaEntry(file=image.name, latex=ocr(image), model=MODEL_NAME)
            save_cache(cache, cache_path)
        latex_by_file[image.name] = cache[key].latex
    return latex_by_file
