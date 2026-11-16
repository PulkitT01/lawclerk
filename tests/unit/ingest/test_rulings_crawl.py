import httpx
import pytest
from conftest import (
    ANLAGE,
    CLOSED,
    DATA_PDF,
    DECISION,
    GZ,
    NOTICE,
    ONGOING,
    PDF,
    SITE,
    TOPIC,
    TOPIC_OTHER,
    TOPIC_SUB,
    VIDEO,
    FakeSite,
    html,
    list_page,
)

from lawclerk.ingest.rulings.crawl import BrokenRunError, crawl, in_boundary
from lawclerk.ingest.rulings.snapshot import Link

pytestmark = pytest.mark.usefixtures("no_backoff")


def test_in_boundary_takes_all_of_bk6_gz_and_bk06() -> None:
    assert in_boundary(f"/{GZ}/x.html")
    assert in_boundary("/DE/Beschlusskammern/BK06/BK6_83_Zug_Mess/a/b.html")
    assert not in_boundary("/DE/Beschlusskammern/BK07/x.html")
    assert not in_boundary("/DE/Beschlusskammern/1_GZ/BK7-GZ/2016/x.html")


def test_crawl_follows_the_boundary(site: FakeSite) -> None:
    with site.client() as client:
        result = crawl(client)
    assert set(result.loaded()) == {
        CLOSED,
        ONGOING,
        DECISION,
        NOTICE,
        TOPIC,
        TOPIC_SUB,
        TOPIC_OTHER,
    }
    assert result.by_depth() == {"0": 2, "1": 2, "2": 3}
    assert set(result.files) == {PDF, ANLAGE, DATA_PDF, VIDEO}
    reasons = {url: reason for url, (reason, _) in result.skipped.items()}
    assert reasons == {
        "https://www.acer.europa.eu/decision.pdf": "off_site",
        f"{SITE}/DE/Vportal/artikel.html": "outside_boundary",
        f"{SITE}/DE/Beschlusskammern/BK07/BK7_04/x.html": "outside_boundary",
    }
    assert not any("Menu" in url for url in site.requests)  # links outside <main> are ignored
    assert all("nn=" not in url for url in site.requests)


def test_crawl_keys_a_redirected_html_link_by_its_target(site: FakeSite) -> None:
    with site.client() as client:
        result = crawl(client)
    assert result.files[ANLAGE] == [
        Link(page=DECISION, text="Anlage 1", href=f"{GZ}/Anlage.html?nn=861698")
    ]
    assert result.redirect_v[ANLAGE] == "2"
    assert f"{SITE}/{GZ}/Anlage.html" not in result.pages


def test_crawl_stops_at_the_page_cap(site: FakeSite) -> None:
    with site.client() as client, pytest.raises(BrokenRunError, match="more than 3 pages"):
        crawl(client, max_pages=3)


def test_crawl_refuses_a_list_with_an_aktenzeichen_twice(site: FakeSite) -> None:
    row = "<tr><td>BK6-1</td><td>a</td><td></td><td></td></tr>"
    site.routes[ONGOING] = lambda _: html(list_page(row + row))
    with site.client() as client, pytest.raises(ValueError, match="BK6-1"):
        crawl(client)


def test_crawl_is_broken_when_a_list_fails(site: FakeSite) -> None:
    site.routes[CLOSED] = lambda _: httpx.Response(500)
    with site.client() as client, pytest.raises(BrokenRunError, match="list page failed"):
        crawl(client)
