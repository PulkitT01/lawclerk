"""Read what the crawl needs from a BNetzA page: base, canonical, <main> links, list rows, sizes."""

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final

SIZE_IN_TEXT: Final = re.compile(r"\(\s*\w+\s*/\s*([\d.,]+)\s*(KB|MB|GB)\s*\)")
SIZE_UNITS: Final = {"KB": 1e3, "MB": 1e6, "GB": 1e9}
# Tags that separate words in a cell or link text, like a line break would
BREAKS: Final = frozenset({"p", "br", "div", "li", "ul", "ol"})


def size_from_text(text: str) -> int | None:
    """Return the size a link text gives, e.g. "Beschluss (pdf / 220 KB)" -> 220000."""
    match = SIZE_IN_TEXT.search(text.replace("\xa0", " "))
    if match is None:
        return None
    number = float(match.group(1).replace(".", "").replace(",", "."))
    return round(number * SIZE_UNITS[match.group(2)])


def main_sha256(body: bytes) -> str | None:
    """Hash a page's <main> element; the menu around it changes without notice."""
    start = body.find(b"<main")
    end = body.find(b"</main>", start)
    return hashlib.sha256(body[start:end]).hexdigest() if 0 <= start < end else None


@dataclass
class Row:
    """One data row of a list table."""

    cells: list[str]
    links: list[tuple[str, str]]  # (href, text)


def squash(text: str) -> str:
    """Collapse all whitespace, the non-breaking space included, to single spaces."""
    return " ".join(text.split())


class PageParser(HTMLParser):
    """Collect what the crawl needs from a page: base, canonical, <main> links, table rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base: str | None = None
        self.canonical: str | None = None
        self.has_main = False
        self.links: list[tuple[str, str]] = []  # (href, text) inside <main>
        self.rows: list[Row] = []  # data rows of the page's first table
        self._in_main = False
        self._anchor: tuple[str, list[str]] | None = None
        self._table = "before"  # before, inside or after the first table
        self._row: Row | None = None
        self._header_row = False
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name: value or "" for name, value in attrs}
        if tag in BREAKS:
            self.handle_data(" ")
        if tag == "base" and self.base is None:
            self.base = attr.get("href")
        elif tag == "link" and attr.get("rel") == "canonical":
            self.canonical = attr.get("href")
        elif tag == "main":
            self.has_main = self._in_main = True
        elif tag == "table" and self._table == "before":
            self._table = "inside"
        elif tag == "tr" and self._table == "inside":
            self._end_row()
            self._row, self._header_row = Row([], []), False
        elif tag in ("td", "th") and self._row is not None:
            self._end_cell()
            self._cell = []
            self._header_row |= tag == "th"
        elif tag == "a" and "href" in attr:
            if self._anchor is not None:
                self.handle_endtag("a")  # a link inside a link closes the first, as in browsers
            self._anchor = (attr["href"].strip(), [])

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor[1].append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in BREAKS:
            self.handle_data(" ")
        if tag == "a" and self._anchor is not None:
            link = (self._anchor[0], squash("".join(self._anchor[1])))
            if self._in_main:
                self.links.append(link)
            if self._row is not None:
                self._row.links.append(link)
            self._anchor = None
        elif tag in ("td", "th"):
            self._end_cell()
        elif tag == "tr":
            self._end_row()
        elif tag == "table" and self._table == "inside":
            self._end_row()
            self._table = "after"
        elif tag == "main":
            self._in_main = False

    def _end_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.cells.append(squash("".join(self._cell)))
        self._cell = None

    def _end_row(self) -> None:
        self._end_cell()
        if self._row is not None and not self._header_row:
            self.rows.append(self._row)
        self._row = None


def parse_page(body: bytes) -> PageParser:
    """Parse a page's HTML (the site serves UTF-8)."""
    parser = PageParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    return parser
