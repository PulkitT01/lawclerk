"""Build the version history of federal statutes and split every version into chunks.

Past versions come from QuantLaw's archive of gesetze-im-internet.de (see archive.py).
The live download from gesetze-im-internet.de only checks that the archive is up to date.

Run from the repository root: ``uv run python -m lawclerk.ingest.statutes``
"""

import io
import logging
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Final, Literal

import httpx
from pydantic import BaseModel

from lawclerk.ingest import archive, formulas

logger = logging.getLogger(__name__)

BASE_URL: Final = "https://www.gesetze-im-internet.de"

# Short name -> the law's slug on gesetze-im-internet.de (as in .../<slug>/xml.zip)
LAWS: Final = {"EnWG": "enwg_2005"}

OUTPUT_DIR: Final = Path("data/raw/statutes")
ARCHIVE_DIR: Final = OUTPUT_DIR / "archive.git"
PROCESSED_DIR: Final = Path("data/processed/statutes")
FORMULAS_FILE: Final = "formulas.json"  # OCR cache of the formula images, inside PROCESSED_DIR

# A snapshot with fewer sections than this share of the previous one is a broken scrape.
MIN_SECTION_RATIO: Final = 0.9

# Warn when the archive's newest scrape is older than this.
MAX_ARCHIVE_AGE: Final = timedelta(days=3)

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
    version_id: str | None = None  # the SectionVersion this chunk was cut from
    law: str
    section: str
    title: str | None
    heading_path: list[str]
    status: Literal["in_force", "repealed"]
    stand_note: str | None
    source_url: str
    absatz: str | None = None
    nummer: str | None = None
    text: str


class SectionVersion(BaseModel):
    """One section's title and text over an unbroken run of archive snapshots.

    first_seen and commit are observed: the first snapshot that had this text. valid_from is
    derived: first_seen, or None when the text was already there in the first snapshot.
    valid_to is exclusive: the first snapshot where the text changed or the section was gone.
    """

    id: str  # "<law> <section>@<first_seen>"
    law: str
    section: str
    title: str | None
    heading_path: list[str]
    status: Literal["in_force", "repealed"]
    text: str
    doknr: str
    stand_note: str | None
    first_seen: date
    commit: str
    valid_from: date | None
    valid_to: date | None
    # Heuristic: an amended section gets a new doknr, a corrected one keeps it
    change: Literal["initial", "amendment", "correction"]
    source_url: str


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
    seen_labels: set[str] = set()
    paragraphs = content.findall("P")
    for position, p in enumerate(paragraphs, start=1):
        absatz_text = clean(render(p))
        if not re.search(r"[^\W_]", absatz_text):  # no letter or digit (\w includes "_")
            continue  # empty, or a separator line such as "__________" above footnotes
        match = ABSATZ_LABEL.match(absatz_text)
        absatz = match.group(1) if match else None
        if absatz in seen_labels:
            logger.warning("%s %s: Absatz (%s) appears twice", whole.law, whole.id, absatz)
            absatz_id = f"{whole.id}-abs{absatz}-p{position}"
        elif absatz:
            seen_labels.add(absatz)
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


def is_repealed(title: str | None) -> bool:
    """Tell whether a section title marks the section as repealed, e.g. "(weggefallen)"."""
    return title is not None and REPEALED_TITLE.fullmatch(title) is not None


def section_text(norm: ET.Element) -> tuple[str | None, str]:
    """Return a section's title and whole text; a repealed section's text is its title."""
    raw_title = norm.findtext("metadaten/titel")
    title = clean(raw_title) if raw_title else None
    if title is not None and is_repealed(title):
        return title, title
    content = norm.find("textdaten/text/Content")
    return title, clean(render(content)) if content is not None else ""


def chunk_norm(
    norm: ET.Element, *, law: str, slug: str, heading_path: list[str], stand_note: str | None
) -> list[Chunk]:
    """Turn one section <norm> into chunks; a repealed section becomes one stub chunk."""
    section = norm.findtext("metadaten/enbez", "")
    title, text = section_text(norm)
    repealed = is_repealed(title)
    whole = Chunk(
        id=norm.attrib["doknr"],
        law=law,
        section=section,
        title=title,
        heading_path=heading_path,
        status="repealed" if repealed else "in_force",
        stand_note=stand_note,
        source_url=section_url(slug, section),
        text="",
    )
    if repealed:
        return [whole.model_copy(update={"text": text})]

    content = norm.find("textdaten/text/Content")
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


def read_folder(folder: Path, law: str) -> ET.Element:
    """Parse the one XML file in folder, with the LaTeX of its formula images put in."""
    (xml_path,) = folder.glob("*.xml")  # each zip and archive folder holds exactly one XML
    root = ET.parse(xml_path).getroot()
    images = formulas.find_images(root, folder)
    latex_by_file = formulas.update_cache(
        images, PROCESSED_DIR / FORMULAS_FILE, formulas.ocr_formula
    )
    insert_formulas(root, latex_by_file, law)
    return root


@dataclass
class Section:
    """One section as it reads in one snapshot. Two are equal when title and text are."""

    title: str | None
    text: str
    norm: ET.Element = field(compare=False)
    heading_path: list[str] = field(compare=False)


def read_sections(root: ET.Element) -> dict[str, Section]:
    """Map each section name in a gesetze-im-internet.de XML file (gii-norm.dtd) to its text.

    A section without text is left out: it counts as absent from the snapshot.
    """
    heading_path: list[str] = []
    sections: dict[str, Section] = {}
    for norm in root.iterfind("norm"):
        unit = norm.find("metadaten/gliederungseinheit")
        if unit is not None:
            # Heading norm (Teil, Abschnitt...): its code has 3 digits per level, e.g. "030010"
            depth = len(unit.findtext("gliederungskennzahl", "")) // 3
            parts = [unit.findtext("gliederungsbez"), unit.findtext("gliederungstitel")]
            label = clean(" ".join(part for part in parts if part))
            heading_path = [*heading_path[: depth - 1], label]
            continue
        name = norm.findtext("metadaten/enbez")
        if name is None or name in SKIPPED_SECTIONS:
            continue  # law-level metadata norm, or table of contents
        title, text = section_text(norm)
        if text:
            # Annexes follow the last heading in the file but don't belong to it
            path = [] if name.startswith("Anlage") else heading_path
            sections[name] = Section(title, text, norm, path)
    return sections


def changed_sections(old: Mapping[str, Section], new: Mapping[str, Section]) -> list[str]:
    """Return the sections whose title or text differ between two snapshots, or only one has."""
    return [name for name in dict.fromkeys([*old, *new]) if old.get(name) != new.get(name)]


@dataclass
class History:
    """The versions of one law's sections, built from its snapshots in date order."""

    versions: list[SectionVersion] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    # Broken snapshots left out: (date, its section count, the previous snapshot's)
    skipped: list[tuple[date, int, int]] = field(default_factory=list)
    # The sections of the newest snapshot that wasn't skipped
    newest: dict[str, Section] = field(default_factory=dict)


def build_history(
    snapshots: Iterable[tuple[str, date, ET.Element]], *, law: str, slug: str, xml_file: str
) -> History:
    """Build the versions of every section from (commit, scrape date, XML root) snapshots.

    A version starts when a section appears or its title or text changes, and ends when it
    changes again or the section is gone. Snapshots must come oldest first. xml_file is the
    archive path of the law's XML, for the source URL of versions that ended.
    """
    history = History()
    norms: list[ET.Element] = []  # the norm each version was first seen in, to chunk it from
    open_versions: dict[str, SectionVersion] = {}
    for commit, day, root in snapshots:
        sections = read_sections(root)
        previous = history.newest
        if previous and len(sections) < MIN_SECTION_RATIO * len(previous):
            logger.warning(
                "%s: skipped snapshot %s, %d of %d sections", law, day, len(sections), len(previous)
            )
            history.skipped.append((day, len(sections), len(previous)))
            continue
        for name in previous.keys() - sections.keys():
            open_versions.pop(name).valid_to = day
        stand_note = law_version(root)
        for name, section in sections.items():
            before = previous.get(name)
            if before == section:
                continue
            doknr = section.norm.attrib["doknr"]
            if before is None:
                change: Literal["initial", "amendment", "correction"] = "initial"
            else:
                open_versions[name].valid_to = day
                same_doknr = before.norm.attrib["doknr"] == doknr
                change = "correction" if same_doknr else "amendment"
            version = SectionVersion(
                id=f"{law} {name}@{day}",
                law=law,
                section=name,
                title=section.title,
                heading_path=section.heading_path,
                status="repealed" if is_repealed(section.title) else "in_force",
                text=section.text,
                doknr=doknr,
                stand_note=stand_note,
                first_seen=day,
                commit=commit,
                valid_from=day if previous else None,  # None: there before the first snapshot
                valid_to=None,
                change=change,
                source_url="",  # set below, once it's known whether the version ended
            )
            open_versions[name] = version
            history.versions.append(version)
            norms.append(section.norm)
        history.newest = sections

    for version, norm in zip(history.versions, norms, strict=True):
        # gesetze-im-internet.de only shows the current text, so a version that ended
        # points at the archive file it was first seen in
        version.source_url = (
            section_url(slug, version.section)
            if version.valid_to is None
            else archive.file_url(version.commit, xml_file)
        )
        chunks = chunk_norm(
            norm,
            law=law,
            slug=slug,
            heading_path=version.heading_path,
            stand_note=version.stand_note,
        )
        history.chunks.extend(
            chunk.model_copy(
                update={
                    "id": f"{chunk.id}@{version.first_seen}",
                    "version_id": version.id,
                    "source_url": version.source_url,
                }
            )
            for chunk in chunks
        )
    return history


def version_at(
    versions: Iterable[SectionVersion], section: str, day: date
) -> SectionVersion | None:
    """Return the version of a section that applied on day, or None.

    A version with an unknown start counts from its first_seen, the archive's first scrape,
    so a day before the archive begins finds nothing.
    """
    for version in versions:
        start = version.valid_from or version.first_seen
        ended = version.valid_to is not None and day >= version.valid_to
        if version.section == section and start <= day and not ended:
            return version
    return None


def archive_snapshots(
    repo: Path, snapshots: Iterable[archive.Snapshot], xml_file: str, law: str
) -> Iterator[tuple[str, date, ET.Element]]:
    """Read each snapshot of a law from the archive, formulas included, one at a time."""
    folder = str(PurePosixPath(xml_file).parent)
    for snapshot in snapshots:
        with tempfile.TemporaryDirectory() as tmp:
            archive.export(repo, snapshot.commit, folder, Path(tmp))
            yield snapshot.commit, snapshot.scraped, read_folder(Path(tmp), law)


def write_jsonl(items: Sequence[Chunk | SectionVersion], path: Path) -> None:
    """Write items to path, one JSON object per line. Refuse to write two with the same id."""
    duplicates = [id_ for id_, count in Counter(item.id for item in items).items() if count > 1]
    if duplicates:
        raise ValueError(f"Duplicate ids, {path} not written: {duplicates}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(item.model_dump_json() + "\n" for item in items), encoding="utf-8")


def main() -> None:
    """Rebuild the version history of every law in LAWS and check it against the live text.

    Writes <slug>.versions.jsonl and <slug>.jsonl (the chunks of every version) to
    PROCESSED_DIR.
    """
    archive.update(ARCHIVE_DIR)
    newest_scrape = archive.newest_scrape(ARCHIVE_DIR)
    if date.today() - newest_scrape > MAX_ARCHIVE_AGE:
        logger.warning("The archive's newest scrape is from %s", newest_scrape)
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for name, slug in LAWS.items():
            xml_file = archive.find_xml(ARCHIVE_DIR, slug)
            snapshots = archive.snapshots(ARCHIVE_DIR, xml_file)
            print(f"{name}: newest archive scrape {newest_scrape}, {len(snapshots)} snapshots")
            history = build_history(
                archive_snapshots(ARCHIVE_DIR, snapshots, xml_file, name),
                law=name,
                slug=slug,
                xml_file=xml_file,
            )
            for day, count, previous in history.skipped:
                print(f"{name}: skipped {day} ({count} of {previous} sections)")
            versions = history.versions
            sections = len({version.section for version in versions})
            still_open = sum(version.valid_to is None for version in versions)
            print(f"{name}: {len(versions)} versions of {sections} sections, {still_open} open")
            # Chunks first: only their ids can clash (version ids are unique per section and
            # snapshot), so a refused write leaves both files from the previous run
            write_jsonl(history.chunks, PROCESSED_DIR / f"{slug}.jsonl")
            write_jsonl(versions, PROCESSED_DIR / f"{slug}.versions.jsonl")
            print(f"{name}: wrote {len(history.chunks)} chunks")

            fetch_law(client, slug, OUTPUT_DIR)
            live = read_sections(read_folder(OUTPUT_DIR / slug, name))
            changed = changed_sections(history.newest, live)
            if changed:
                logger.warning(
                    "%s: live text differs from the newest snapshot in %s", name, ", ".join(changed)
                )
            else:
                print(f"{name}: live text matches the newest snapshot")


if __name__ == "__main__":
    main()
