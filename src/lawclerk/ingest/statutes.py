"""Download federal statutes from gesetze-im-internet.de and split them into chunks.

Run from the repository root: ``uv run python -m lawclerk.ingest.statutes``
"""

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

import httpx
from pydantic import BaseModel

from lawclerk.ingest import formulas

logger = logging.getLogger(__name__)

BASE_URL: Final = "https://www.gesetze-im-internet.de"

# Short name -> the law's slug on gesetze-im-internet.de (as in .../<slug>/xml.zip)
LAWS: Final = {"EnWG": "enwg_2005"}

OUTPUT_DIR: Final = Path("data/raw/statutes")
PROCESSED_DIR: Final = Path("data/processed/statutes")
FORMULAS_FILE: Final = "formulas.json"  # OCR cache of the formula images, inside PROCESSED_DIR

# A section longer than this is split by Absatz; an Absatz longer than this, by Nummer.
MAX_CHUNK_CHARS: Final = 8000

# Norms that duplicate other content (the table of contents) and aren't chunked.
SKIPPED_SECTIONS: Final = frozenset({"Inhaltsübersicht"})

REPEALED_TITLE: Final = re.compile(r"\((weggefallen|aufgehoben)\)")
ABSATZ_LABEL: Final = re.compile(r"\((\d+[a-z]*)\)")
SECTION_NUMBER: Final = re.compile(r"§ (\w+)")

# Elements whose content starts on a new line when rendered as text.
BLOCK_TAGS: Final = frozenset({"P", "LA", "Footnotes"})

# Subscript/superscript rendered LaTeX-style: KA<SUB>dnb,t</SUB> -> KA_{dnb,t}
SCRIPT_MARKERS: Final = {"SUB": "_", "SUP": "^"}


class Chunk(BaseModel):
    """One retrievable piece of a statute: a whole section, one Absatz, or one Nummer."""

    id: str
    law: str
    section: str
    title: str | None
    heading_path: list[str]
    status: Literal["in_force", "repealed"]
    version: str | None
    source_url: str
    absatz: str | None = None
    nummer: str | None = None
    text: str


def download_zip(client: httpx.Client, slug: str) -> bytes:
    """Download the XML zip archive for one law."""
    response = client.get(f"{BASE_URL}/{slug}/xml.zip")
    response.raise_for_status()
    return response.content


def save_if_changed(path: Path, data: bytes) -> bool:
    """Write data to path unless the file there has the same bytes. Return True if written."""
    if path.exists() and path.read_bytes() == data:
        return False
    path.write_bytes(data)
    return True


def extract_zip(archive: bytes, dest: Path) -> list[Path]:
    """Extract every file in archive into dest. Return the paths that were new or changed.

    Files in dest that are not in the archive are deleted, so dest holds only the latest
    version of the law.
    """
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = zf.namelist()
        for name in names:
            # The archives are flat; anything with a directory part (e.g. "../x") is refused
            # so a malformed archive can't write outside dest.
            if Path(name).name != name:
                raise ValueError(f"Unexpected path in archive: {name!r}")
            path = dest / name
            if save_if_changed(path, zf.read(name)):
                written.append(path)
    for path in dest.iterdir():
        if path.is_file() and path.name not in names:
            logger.info("Deleting %s: no longer in the archive", path)
            path.unlink()
    return written


def fetch_law(client: httpx.Client, slug: str, output_dir: Path) -> list[Path]:
    """Download one law and extract it into output_dir/<slug>/. Return new or changed files."""
    return extract_zip(download_zip(client, slug), output_dir / slug)


def clean(text: str) -> str:
    """Collapse whitespace within each line and drop empty lines."""
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def render(el: ET.Element) -> str:
    """Return the text inside el, with paragraphs, list items and table rows on their own lines.

    The result still needs clean() to tidy whitespace.
    """
    parts = [el.text or ""]
    for child in el:
        if child.tag == "DL":
            parts.append(render_list(child))
        elif child.tag == "row":
            # Empty cells are kept so values stay in their column
            parts.append("\n" + " | ".join(clean(render(entry)) for entry in child) + "\n")
        elif child.tag in SCRIPT_MARKERS:
            parts.append(SCRIPT_MARKERS[child.tag] + "{" + render(child) + "}")
        elif child.tag in BLOCK_TAGS:
            parts.append("\n" + render(child) + "\n")
        elif child.tag == "BR":
            parts.append("\n")
        else:  # inline markup such as <B>, <SUB>: glued to the surrounding text
            parts.append(render(child))
        # Text after a child's closing tag is stored on the child, not on el
        parts.append(child.tail or "")
    return "".join(parts)


def render_list(dl: ET.Element) -> str:
    """Render a <DL> list as one line per item: the label (<DT>), then the item (<DD>)."""
    parts = []
    for child in dl:
        if child.tag == "DT":
            parts.append("\n" + clean(render(child)) + " ")
        elif child.tag == "DD":
            parts.append(render(child).lstrip())
    return "".join(parts) + "\n"


def split_nummern(p: ET.Element) -> list[tuple[str, str]] | None:
    """Split an Absatz into (nummer, text) pairs, repeating its intro sentence in each.

    Only handles the shape seen in the data: intro text followed by a single list.
    Returns None for any other shape, so no text is silently dropped.
    """
    children = list(p)
    if len(children) != 1 or children[0].tag != "DL" or (children[0].tail or "").strip():
        return None
    dl = children[0]
    intro = p.text or ""
    pairs = []
    for dt, dd in zip(dl.findall("DT"), dl.findall("DD"), strict=True):
        label = clean(render(dt))
        pairs.append((label.rstrip("."), clean(f"{intro}\n{label} {render(dd).lstrip()}")))
    return pairs


def split_section(content: ET.Element, text: str, whole: Chunk) -> list[Chunk]:
    """Split a section's content into chunks: whole, else by Absatz, else by Nummer."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [whole.model_copy(update={"text": text})]
    # Only <P> elements are split out, so text anywhere else would be lost: keep it whole
    outside_paragraphs = bool((content.text or "").strip()) or any(
        child.tag not in ("P", "BR") or (child.tail or "").strip() for child in content
    )
    if outside_paragraphs:
        logger.warning("%s %s: oversized section has text outside its Absätze", whole.law, whole.id)
        return [whole.model_copy(update={"text": text})]

    chunks: list[Chunk] = []
    paragraphs = content.findall("P")
    for position, p in enumerate(paragraphs, start=1):
        absatz_text = clean(render(p))
        if not re.search(r"[^\W_]", absatz_text):  # no letter or digit (\w includes "_")
            continue  # empty, or a separator line such as "__________" above footnotes
        match = ABSATZ_LABEL.match(absatz_text)
        absatz = match.group(1) if match else None
        if absatz:
            absatz_id = f"{whole.id}-abs{absatz}"
        elif len(paragraphs) > 1:
            absatz_id = f"{whole.id}-p{position}"  # unnumbered Absatz among several
        else:
            absatz_id = whole.id  # single unnumbered Absatz, e.g. § 3 EnWG
        piece = whole.model_copy(update={"id": absatz_id, "absatz": absatz, "text": absatz_text})

        if len(absatz_text) <= MAX_CHUNK_CHARS:
            chunks.append(piece)
            continue
        nummern = split_nummern(p)
        if nummern is None:
            logger.warning("%s %s: oversized Absatz has no list to split on", whole.law, piece.id)
            chunks.append(piece)
            continue
        for nummer, nummer_text in nummern:
            if len(nummer_text) > MAX_CHUNK_CHARS:
                logger.warning("%s %s Nr. %s: oversized Nummer", whole.law, piece.id, nummer)
            chunks.append(
                piece.model_copy(
                    update={"id": f"{absatz_id}-nr{nummer}", "nummer": nummer, "text": nummer_text}
                )
            )
    return chunks


def section_url(slug: str, section: str) -> str:
    """Return the web page of a section ("§ 23a" -> .../__23a.html), or the law's index page."""
    match = SECTION_NUMBER.fullmatch(section)
    page = f"__{match.group(1)}.html" if match else "index.html"
    return f"{BASE_URL}/{slug}/{page}"


def law_version(root: ET.Element) -> str | None:
    """Return the law's "Stand" note (e.g. "Zuletzt geändert durch ..."), if it has one."""
    for stand in root.iterfind("norm/metadaten/standangabe"):
        if stand.findtext("standtyp") == "Stand":
            return stand.findtext("standkommentar")
    return None


def chunk_norm(
    norm: ET.Element, *, law: str, slug: str, heading_path: list[str], version: str | None
) -> list[Chunk]:
    """Turn one section <norm> into chunks; a repealed section becomes one stub chunk."""
    section = norm.findtext("metadaten/enbez", "")
    raw_title = norm.findtext("metadaten/titel")
    title = clean(raw_title) if raw_title else None
    repealed = title is not None and REPEALED_TITLE.fullmatch(title) is not None
    whole = Chunk(
        id=norm.attrib["doknr"],
        law=law,
        section=section,
        title=title,
        heading_path=heading_path,
        status="repealed" if repealed else "in_force",
        version=version,
        source_url=section_url(slug, section),
        text="",
    )
    if repealed:
        return [whole.model_copy(update={"text": title})]

    content = norm.find("textdaten/text/Content")
    text = clean(render(content)) if content is not None else ""
    if content is None or not text:
        logger.warning("%s %s: skipped, no text", law, section)
        return []
    return split_section(content, text, whole)


def insert_formulas(root: ET.Element, latex_by_file: Mapping[str, str], law: str) -> None:
    """Put the LaTeX of each formula image where the image stands in the XML.

    render() emits an element's text, so the <IMG> element is given the formula as its text,
    with the delimiters the OCR model returned. An image without a formula is left empty
    and logged.
    """
    for img in root.iter("IMG"):
        name = Path(img.get("SRC", "")).name
        latex = latex_by_file.get(name)
        if latex is None:
            logger.warning("%s: no formula for image %r", law, name)
        else:
            img.text = latex


def parse_statute(
    root: ET.Element, law: str, slug: str, latex_by_file: Mapping[str, str]
) -> list[Chunk]:
    """Turn the root of a gesetze-im-internet.de XML file (gii-norm.dtd) into chunks.

    latex_by_file maps the file name of each formula image to its LaTeX.
    """
    insert_formulas(root, latex_by_file, law)
    version = law_version(root)
    heading_path: list[str] = []
    chunks: list[Chunk] = []
    for norm in root.iterfind("norm"):
        unit = norm.find("metadaten/gliederungseinheit")
        if unit is not None:
            # Heading norm (Teil, Abschnitt...): its code has 3 digits per level, e.g. "030010"
            depth = len(unit.findtext("gliederungskennzahl", "")) // 3
            parts = [unit.findtext("gliederungsbez"), unit.findtext("gliederungstitel")]
            label = clean(" ".join(part for part in parts if part))
            heading_path = [*heading_path[: depth - 1], label]
            continue
        section = norm.findtext("metadaten/enbez")
        if section is None or section in SKIPPED_SECTIONS:
            continue  # law-level metadata norm, or table of contents
        # Annexes follow the last heading in the file but don't belong to it
        path = [] if section.startswith("Anlage") else heading_path
        chunks.extend(chunk_norm(norm, law=law, slug=slug, heading_path=path, version=version))
    return chunks


def write_jsonl(chunks: list[Chunk], path: Path) -> None:
    """Write chunks to path, one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(chunk.model_dump_json() + "\n" for chunk in chunks), encoding="utf-8")


def main() -> None:
    """Fetch every law in LAWS into OUTPUT_DIR and write its chunks to PROCESSED_DIR."""
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for name, slug in LAWS.items():
            written = fetch_law(client, slug, OUTPUT_DIR)
            if written:
                print(f"{name}: updated {', '.join(p.name for p in written)}")
            else:
                print(f"{name}: unchanged")

            (xml_path,) = (OUTPUT_DIR / slug).glob("*.xml")  # each zip holds exactly one XML
            root = ET.parse(xml_path).getroot()
            images = formulas.find_images(root, xml_path.parent)
            latex_by_file = formulas.update_cache(
                images, PROCESSED_DIR / FORMULAS_FILE, formulas.ocr_formula
            )
            chunks = parse_statute(root, name, slug, latex_by_file)
            write_jsonl(chunks, PROCESSED_DIR / f"{slug}.jsonl")
            print(f"{name}: wrote {len(chunks)} chunks")


if __name__ == "__main__":
    main()
