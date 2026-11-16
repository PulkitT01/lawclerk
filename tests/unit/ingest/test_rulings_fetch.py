import httpx
import pytest
from conftest import (
    SITE,
)

from lawclerk.ingest.rulings import fetch

pytestmark = pytest.mark.usefixtures("no_backoff")


def test_url_key_drops_query_and_fragment_and_uses_https() -> None:
    assert fetch.url_key("http://www.bundesnetzagentur.de/a/b.html?nn=1#x") == f"{SITE}/a/b.html"
    assert fetch.url_key("http://www.bundesnetzagentur.de") == f"{SITE}/"


def test_query_v_reads_the_v_parameter() -> None:
    assert fetch.query_v("/a.pdf?__blob=publicationFile&v=3") == "3"
    assert fetch.query_v("/a.pdf?__blob=publicationFile") is None


@pytest.mark.parametrize(
    ("path", "is_page"),
    [
        ("/a/b.html", True),
        ("/", True),
        ("/a/start", True),
        ("/a/b.pdf", False),
        ("/a/b.mp4", False),
    ],
)
def test_is_page_path(path: str, is_page: bool) -> None:
    assert fetch.is_page_path(path) is is_page


def test_with_retries_retries_then_gives_up() -> None:
    attempts: list[int] = []

    def failing() -> str:
        attempts.append(1)
        raise httpx.ConnectError("down")

    with pytest.raises(fetch.FetchError):
        fetch.with_retries(failing, "u")
    assert len(attempts) == fetch.MAX_RETRIES + 1


def test_get_retries_overload_and_detects_the_firewall_page() -> None:
    answers = iter([httpx.Response(503), httpx.Response(200, text="ok")])
    with httpx.Client(transport=httpx.MockTransport(lambda _: next(answers))) as client:
        assert fetch.get(client, f"{SITE}/x.html").text == "ok"
    rejected = httpx.Response(200, text="<html>The requested URL was rejected.</html>")
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: rejected)) as client,
        pytest.raises(fetch.FirewallError),
    ):
        fetch.get(client, f"{SITE}/x.html")
