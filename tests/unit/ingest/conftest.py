import os
import subprocess
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from lawclerk.ingest import rulings
from lawclerk.ingest.rulings import RowLine


class SourceArchive:
    """A local git repository shaped like QuantLaw's archive, to clone instead of GitHub."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.run("init", "--quiet", "--initial-branch=data")

    def run(self, *args: str, env: dict[str, str] | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
            text=True,
            env=os.environ | (env or {}),
        ).stdout

    def commit(self, day: str, files: dict[str, bytes], delete: tuple[str, ...] = ()) -> str:
        """Write files (paths relative to the repo), delete others, commit as scraped on day."""
        for name, data in files.items():
            path = self.path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for name in delete:
            (self.path / name).unlink()
        self.run("add", "--all")
        stamp = f"{day}T04:00:00"
        env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": "2020-05-02T00:00:00"}
        self.run("commit", "--quiet", "--allow-empty", "-m", f"scrape {stamp}", env=env)
        return self.run("rev-parse", "HEAD").strip()


@pytest.fixture
def source_archive(tmp_path: Path) -> SourceArchive:
    path = tmp_path / "source"
    path.mkdir()
    return SourceArchive(path)


# --- The BK6 site in miniature, for the rulings tests ------------------------------


SITE = "https://www.bundesnetzagentur.de"

DATA = "https://data.bundesnetzagentur.de/Bundesnetzagentur"

GZ = "DE/Beschlusskammern/1_GZ/BK6-GZ/2026/BK6-26-001"

CLOSED, ONGOING = rulings.LISTS

DECISION = f"{SITE}/{GZ}/BK6-26-001_beschluss.html"

NOTICE = f"{SITE}/{GZ}/Mitteilung/Mitteilung_Nr_1.html"

TOPIC = f"{SITE}/DE/Beschlusskammern/BK06/BK6_77_LiRaVer/LiRaV.html"

TOPICS = "DE/Beschlusskammern/BK06"

TOPIC_SUB = f"{SITE}/{TOPICS}/BK6_77_LiRaVer/Sub/Seite.html"

TOPIC_OTHER = f"{SITE}/{TOPICS}/BK6_83_Zug_Mess/gpke.html"  # a BK06 folder no row links to

PDF = f"{SITE}/{GZ}/BK6-26-001_beschluss.pdf"

ANLAGE = f"{SITE}/{GZ}/Anlage.pdf"

DATA_PDF = f"{DATA}/{GZ}/gutachten.pdf"

VIDEO = f"{DATA}/{GZ}/erklaerung.mp4"

T0 = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)

Handler = Callable[[httpx.Request], httpx.Response]


def page(main: str, nav: str = "") -> str:
    """A BNetzA-shaped page: <base href="/">, canonical link, menu outside <main>."""
    return (
        '<html><head><base href="/"><link rel="canonical" href="http://x/y.html"></head>'
        f'<body><nav><a href="DE/Menu/item.html">Menü</a>{nav}</nav>'
        f"<main>{main}</main></body></html>"
    )


def list_page(rows: str) -> str:
    header = (
        "<tr><th>Aktenzeichen</th><th>Verfahren</th><th>Antragsteller</th><th>Betroffener</th></tr>"
    )
    return page(f"<table><thead>{header}</thead><tbody>{rows}</tbody></table>")


def redirect(location: str) -> httpx.Response:
    return httpx.Response(301, headers={"location": location})


def html(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": "text/html;charset=utf-8"})


def pdf(content: bytes) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": "application/pdf"})


class FakeSite:
    """The BK6 site in miniature, with a log of every request."""

    def __init__(self) -> None:
        closed_rows = (
            "<tr><td>BK6-26-001</td>"
            f'<td><a href="{GZ}/BK6-26-001_beschluss.html?nn=861698">Festlegung\xa0zu X</a></td>'
            "<td>Netz GmbH</td><td></td></tr>"
            "<tr><td>BK6-17-168</td>"
            '<td><a href="DE/Beschlusskammern/BK06/BK6_77_LiRaVer/LiRaV.html">LiRaV</a></td>'
            "<td>-</td><td></td></tr>"
        )
        self.routes: dict[str, Handler] = {
            CLOSED: lambda _: html(list_page(closed_rows)),
            ONGOING: lambda _: html(
                list_page("<tr><td>BK6-26-500</td><td>Antrag</td><td>A</td><td>B</td></tr>")
            ),
            DECISION: lambda _: html(page(self.decision_main())),
            NOTICE: lambda _: html(page(f'<a href="{DATA_PDF}">Gutachten (pdf / 2,5 MB)</a>')),
            TOPIC: lambda _: html(
                page(
                    f'<a href="{TOPICS}/BK6_77_LiRaVer/Sub/Seite.html">Unterseite</a>'
                    f'<a href="{TOPICS}/BK6_83_Zug_Mess/gpke.html">GPKE</a>'
                    '<a href="DE/Beschlusskammern/BK07/BK7_04/x.html">BK7</a>'
                )
            ),
            TOPIC_SUB: lambda _: html(page("<p>Text</p>")),
            TOPIC_OTHER: lambda _: html(page("<p>GPKE</p>")),
            f"{SITE}/{GZ}/Anlage.html": lambda _: redirect(
                f"/{GZ}/Anlage.pdf?__blob=publicationFile&v=2"
            ),
            PDF: lambda _: redirect(
                f"/{GZ}/BK6-26-001_beschluss.pdf?__blob=publicationFile&v={self.pdf_v}"
            ),
            ANLAGE: lambda _: redirect(f"/{GZ}/Anlage.pdf?__blob=publicationFile&v=2"),
            f"{PDF}?__blob=publicationFile&v=1": lambda _: pdf(b"%PDF beschluss v1"),
            f"{PDF}?__blob=publicationFile&v=3": lambda _: pdf(b"%PDF beschluss v3"),
            f"{ANLAGE}?__blob=publicationFile&v=2": lambda _: pdf(b"%PDF anlage"),
            DATA_PDF: self.data_file,
            VIDEO: lambda _: httpx.Response(
                200,
                content=b"x" * 10,
                headers={"content-type": "video/mp4", "content-length": "336000000"},
            ),
        }
        self.pdf_v = 1
        self.decision_links = [
            f'<a href="/{GZ}/BK6-26-001_beschluss.pdf?__blob=publicationFile&amp;v=1">'
            "Beschluss (pdf\xa0/ 220 KB)</a>",
            f'<a href="{GZ}/Anlage.html?nn=861698">Anlage 1</a>',
            f'<a href="{GZ}/Mitteilung/Mitteilung_Nr_1.html?nn=861698">Mitteilung Nr. 1</a>',
            f'<a href="{GZ}/BK6-26-001_beschluss.html?nn=861698#text">Sprung</a>',
            '<a href="https://www.acer.europa.eu/decision.pdf">ACER</a>',
            '<a href="DE/Vportal/artikel.html?nn=1">Verbraucherportal</a>',
            f'<a href="{VIDEO}">Video</a>',
        ]
        self.requests: list[str] = []
        self.responses: list[tuple[str, int]] = []
        self.data_etag: str | None = '"f619a2703fcda1:0"'
        self.data_content = b"%PDF gutachten"

    def data_file(self, request: httpx.Request) -> httpx.Response:
        """The data host: a real ETag, and 304 when asked with it."""
        if self.data_etag and request.headers.get("if-none-match") == self.data_etag:
            return httpx.Response(304)
        response = pdf(self.data_content)
        if self.data_etag:
            response.headers["etag"] = self.data_etag
            response.headers["last-modified"] = "Tue, 31 Oct 2023 21:15:57 GMT"
        return response

    def decision_main(self) -> str:
        return "<h1>Beschluss</h1>" + "".join(self.decision_links)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        route = self.routes.get(url)
        response = route(request) if route else httpx.Response(404, text="nicht gefunden")
        self.responses.append((url, response.status_code))
        return response

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def forget(self) -> None:
        """Start the request log afresh, e.g. before a second run."""
        self.requests.clear()
        self.responses.clear()

    def downloads(self) -> Counter[str]:
        """Count the file bodies served (a query, or the data host; 200, not 304)."""
        return Counter(
            url
            for url, status in self.responses
            if status == 200 and ("__blob" in url or url.startswith(DATA))
        )


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without waiting; the rulings test modules use it for every test."""
    monkeypatch.setattr(rulings, "BACKOFF_SECONDS", 0.0)


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


def run_once(
    site: FakeSite, root: Path, when: datetime = T0, said: list[str] | None = None
) -> rulings.RunHeader:
    with site.client() as client:
        return rulings.ingest(client, root, when, (said if said is not None else []).append)


def lines(root: Path, when: datetime = T0) -> list[rulings.AnyLine]:
    return rulings.read_snapshot(rulings.snapshot_path(root, rulings.run_id(when))).lines


def by_url(root: Path, when: datetime = T0) -> dict[str, rulings.AnyLine]:
    return {line.url: line for line in lines(root, when) if not isinstance(line, RowLine)}


def stored_files(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if path.is_file() and rulings.RUNS not in path.parts}
