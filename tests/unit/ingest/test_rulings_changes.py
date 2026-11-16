from datetime import timedelta

import pytest
from conftest import (
    PDF,
    SITE,
    T0,
)

from lawclerk.ingest.rulings import changes, parse, snapshot
from lawclerk.ingest.rulings.crawl import Crawl, Page
from lawclerk.ingest.rulings.snapshot import FileLine, Link, SkippedLine

pytestmark = pytest.mark.usefixtures("no_backoff")


def known_files(count: int) -> changes.Previous:
    """A last good run at T0 with count files, file 1 checked longest ago."""
    previous = changes.Previous(run=snapshot.run_id(T0))
    stored = snapshot.Stored(path="p", sha256="s", run="r")
    for number in range(1, count + 1):
        url = f"{SITE}/f{number:02d}.pdf"
        checked = snapshot.run_id(T0 - timedelta(days=count - number))
        previous.files[url] = FileLine(
            seen="observed",
            url=url,
            found_on=[],
            v="1",
            hash="checked",
            checked=checked,
            content_type=None,
            bytes=1,
            stored=stored,
            first_seen="r",
            baseline=True,
        )
    return previous


@pytest.mark.parametrize(
    ("since", "due"),
    [
        (timedelta(hours=1), 0),
        (timedelta(days=1), 0),
        (timedelta(days=3), 2),
        (timedelta(days=30), 20),
        (timedelta(days=120), 60),
    ],
)
def test_rotation_share_follows_the_time_since_the_last_good_run(
    since: timedelta, due: int
) -> None:
    previous = known_files(60)
    picked = changes.rotation(
        previous, [*previous.files, f"{SITE}/new.pdf"], snapshot.run_id(T0 + since)
    )
    assert picked == {f"{SITE}/f{number:02d}.pdf" for number in range(1, due + 1)}


def test_rotation_before_the_first_good_run_picks_nothing() -> None:
    assert changes.rotation(changes.Previous(), [PDF], snapshot.run_id(T0)) == set()


def crawl_of(loaded: dict[str, set[str]], failed: tuple[str, ...] = ()) -> Crawl:
    """A crawl result: pages that loaded with the hrefs they show, and pages that failed."""
    result = Crawl(shown=loaded)
    for url in loaded:
        result.pages[url] = Page(url, 1, [], parsed=parse.parse_page(b"<main></main>"))
    for url in failed:
        result.pages[url] = Page(url, 1, [], failed="HTTP 500")
    return result


def test_kept_links_drops_only_links_a_loaded_page_no_longer_shows() -> None:
    result = crawl_of({"A": {"still.html"}}, failed=("B",))
    still, dropped = Link(page="A", text="", href="still.html"), Link(page="A", text="", href="x")
    on_failed, unreached = Link(page="B", text="", href="y"), Link(page="C", text="", href="z")
    assert changes.kept_links(result, [still, dropped, on_failed]) == (
        [still, on_failed],
        "page_failed",
    )
    assert changes.kept_links(result, [unreached]) == ([unreached], "page_not_reached")
    assert changes.kept_links(result, [dropped]) == ([], None)


def test_unseen_turns_a_line_gone_or_carried() -> None:
    line = SkippedLine(
        seen="observed",
        url="u",
        found_on=[Link(page="A", text="", href="x")],
        reason="off_site",
        first_seen="r0",
        baseline=True,
    )
    gone = changes.unseen(crawl_of({"A": set()}), line, "r1")
    assert (gone.seen, gone.gone_since) == ("gone", "r1")
    carried = changes.unseen(crawl_of({}, failed=("A",)), line, "r1")
    assert (carried.seen, carried.carried_because) == ("carried", "page_failed")


def test_origin_keeps_first_seen_and_marks_the_first_run_as_baseline() -> None:
    assert changes.origin(None, "r1", None) == {"first_seen": "r1", "baseline": True}
    assert changes.origin(None, "r2", "r1") == {"first_seen": "r2", "baseline": False}


def test_check_counts_returns_the_rule_that_breaks_the_run() -> None:
    previous = changes.Previous(
        run="r0", header=snapshot.RunHeader(run="r0", status="ok", previous=None, rows=10)
    )
    result = crawl_of({"A": set()})
    result.rows = [("list", parse.Row(["x"], []))] * 8
    broken = changes.check_counts(result, previous)
    assert broken is not None and broken.rule == "too_few_rows"
    result.rows = [("list", parse.Row(["x"], []))] * 9
    assert changes.check_counts(result, previous) is None
