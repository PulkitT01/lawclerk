from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Literal

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
    T0,
    VIDEO,
    FakeSite,
    by_url,
    html,
    lines,
    list_page,
    page,
    pdf,
    run_once,
    stored_files,
)

from lawclerk.ingest import rulings
from lawclerk.ingest.rulings import fetch, parse, snapshot, store
from lawclerk.ingest.rulings.crawl import Crawl, Page
from lawclerk.ingest.rulings.snapshot import FileLine, Link, PageLine, RowLine, SkippedLine

# --- URLs, paths, text ---------------------------------------------------------------------

pytestmark = pytest.mark.usefixtures("no_backoff")


# --- Parsing -------------------------------------------------------------------------------


# --- Snapshots and storage ------------------------------------------------------------------


def known_files(count: int) -> rulings.Previous:
    """A last good run at T0 with count files, file 1 checked longest ago."""
    previous = rulings.Previous(run=snapshot.run_id(T0))
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
    picked = rulings.rotation(
        previous, [*previous.files, f"{SITE}/new.pdf"], snapshot.run_id(T0 + since)
    )
    assert picked == {f"{SITE}/f{number:02d}.pdf" for number in range(1, due + 1)}


def test_rotation_before_the_first_good_run_picks_nothing() -> None:
    assert rulings.rotation(rulings.Previous(), [PDF], snapshot.run_id(T0)) == set()


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
    assert rulings.kept_links(result, [still, dropped, on_failed]) == (
        [still, on_failed],
        "page_failed",
    )
    assert rulings.kept_links(result, [unreached]) == ([unreached], "page_not_reached")
    assert rulings.kept_links(result, [dropped]) == ([], None)


def test_unseen_turns_a_line_gone_or_carried() -> None:
    line = SkippedLine(
        seen="observed",
        url="u",
        found_on=[Link(page="A", text="", href="x")],
        reason="off_site",
        first_seen="r0",
        baseline=True,
    )
    gone = rulings.unseen(crawl_of({"A": set()}), line, "r1")
    assert (gone.seen, gone.gone_since) == ("gone", "r1")
    carried = rulings.unseen(crawl_of({}, failed=("A",)), line, "r1")
    assert (carried.seen, carried.carried_because) == ("carried", "page_failed")


def test_origin_keeps_first_seen_and_marks_the_first_run_as_baseline() -> None:
    assert rulings.origin(None, "r1", None) == {"first_seen": "r1", "baseline": True}
    assert rulings.origin(None, "r2", "r1") == {"first_seen": "r2", "baseline": False}


def test_check_counts_returns_the_rule_that_breaks_the_run() -> None:
    previous = rulings.Previous(
        run="r0", header=snapshot.RunHeader(run="r0", status="ok", previous=None, rows=10)
    )
    result = crawl_of({"A": set()})
    result.rows = [("list", parse.Row(["x"], []))] * 8
    broken = rulings.check_counts(result, previous)
    assert broken is not None and broken.rule == "too_few_rows"
    result.rows = [("list", parse.Row(["x"], []))] * 9
    assert rulings.check_counts(result, previous) is None


def test_summary_gives_the_age_of_the_oldest_check() -> None:
    header = snapshot.RunHeader(
        run="r", status="ok", previous=None, oldest_check="2026-10-17T09:00:00Z"
    )
    assert "oldest check 30 days ago" in rulings.summary(header, T0)


# --- Crawl ---------------------------------------------------------------------------------


# --- Runs ----------------------------------------------------------------------------------


def test_first_run_stores_everything_as_baseline(site: FakeSite, tmp_path: Path) -> None:
    said: list[str] = []
    header = run_once(site, tmp_path, said=said)
    assert header.status == "ok"
    assert header.pages_by_depth == {"0": 2, "1": 2, "2": 3}
    assert said == ["rulings: first download, 4 files: at least 3 MB, 2 of them of unknown size"]
    found = by_url(tmp_path)
    pdf_line = found[PDF]
    assert isinstance(pdf_line, FileLine)
    assert (pdf_line.v, pdf_line.hash, pdf_line.baseline) == ("1", "checked", True)
    assert (tmp_path / pdf_line.stored.path).read_bytes() == b"%PDF beschluss v1"
    assert pdf_line.stored.run == pdf_line.first_seen == snapshot.run_id(T0)
    assert all(line.baseline for line in lines(tmp_path))
    video = found[VIDEO]
    assert isinstance(video, SkippedLine)
    assert (video.reason, video.content_type, video.bytes) == ("video", "video/mp4", 336_000_000)
    assert found["https://www.acer.europa.eu/decision.pdf"].kind == "skipped"
    rows = [line for line in lines(tmp_path) if isinstance(line, RowLine)]
    assert rows[0].cells == ["BK6-26-001", "Festlegung zu X", "Netz GmbH", ""]
    assert rows[0].links[0].href == f"{GZ}/BK6-26-001_beschluss.html?nn=861698"
    assert snapshot.journal_path(tmp_path, snapshot.run_id(T0)).is_file()  # kept, not deleted


def test_second_run_downloads_nothing_and_changes_only_the_record(
    site: FakeSite, tmp_path: Path
) -> None:
    run_once(site, tmp_path)
    files_before = {path: path.read_bytes() for path in stored_files(tmp_path)}
    site.forget()
    header = run_once(site, tmp_path, T0 + timedelta(hours=1))
    assert header.status == "ok"
    assert site.downloads() == Counter()
    assert {path: path.read_bytes() for path in stored_files(tmp_path)} == files_before
    assert (header.files_checked, header.files_carried, header.files_stored) == (0, 3, 0)
    assert header.files_not_modified == 1  # the data-host file, asked with its ETag
    pdf_line = by_url(tmp_path, T0 + timedelta(hours=1))[PDF]
    assert isinstance(pdf_line, FileLine)
    assert (pdf_line.hash, pdf_line.checked) == ("carried", snapshot.run_id(T0))
    assert pdf_line.baseline


def test_changed_v_stores_the_new_copy_beside_the_old(site: FakeSite, tmp_path: Path) -> None:
    run_once(site, tmp_path)
    old = by_url(tmp_path)[PDF]
    site.pdf_v = 3  # the link on the page still shows v=1
    later = T0 + timedelta(days=1)
    header = run_once(site, tmp_path, later)
    new = by_url(tmp_path, later)[PDF]
    assert isinstance(old, FileLine) and isinstance(new, FileLine)
    assert (new.v, new.stored.run, new.first_seen) == ("3", snapshot.run_id(later), old.first_seen)
    assert (tmp_path / old.stored.path).read_bytes() == b"%PDF beschluss v1"
    assert (tmp_path / new.stored.path).read_bytes() == b"%PDF beschluss v3"
    assert header.v_differs == 1
    assert header.files_stored == 1


def test_a_link_its_page_no_longer_shows_is_gone_and_its_file_stays(
    site: FakeSite, tmp_path: Path
) -> None:
    run_once(site, tmp_path)
    old = by_url(tmp_path)[PDF]
    site.decision_links.pop(0)
    later = T0 + timedelta(days=1)
    header = run_once(site, tmp_path, later)
    gone = by_url(tmp_path, later)[PDF]
    assert isinstance(old, FileLine) and isinstance(gone, FileLine)
    assert (gone.seen, gone.gone_since) == ("gone", snapshot.run_id(later))
    assert (tmp_path / old.stored.path).is_file()
    assert header.gone == 1
    even_later = later + timedelta(days=1)
    run_once(site, tmp_path, even_later)
    assert by_url(tmp_path, even_later)[PDF].seen == "gone"  # kept in every later snapshot


def test_a_failed_page_says_nothing_about_its_links(
    site: FakeSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rulings, "MIN_RATIO", 0.5)  # 5 of 6 pages on this tiny site
    run_once(site, tmp_path)
    site.routes[NOTICE] = lambda _: httpx.Response(500)
    later = T0 + timedelta(days=1)
    header = run_once(site, tmp_path, later)
    found = by_url(tmp_path, later)
    assert header.status == "partial"
    assert (found[NOTICE].seen, found[NOTICE].carried_because) == ("carried", "page_failed")
    assert (found[DATA_PDF].seen, found[DATA_PDF].carried_because) == ("carried", "page_failed")
    assert header.gone == 0


def test_a_page_nobody_reaches_is_carried_with_its_files(
    site: FakeSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rulings, "MIN_RATIO", 0.5)  # 5 of 6 pages on this tiny site
    run_once(site, tmp_path)
    site.decision_links = [link for link in site.decision_links if "Mitteilung" not in link]
    later = T0 + timedelta(days=1)
    header = run_once(site, tmp_path, later)
    found = by_url(tmp_path, later)
    notice, data_pdf = found[NOTICE], found[DATA_PDF]
    assert isinstance(notice, PageLine)
    assert (notice.seen, notice.carried_because, notice.found_on) == (
        "carried",
        "page_not_reached",
        [],
    )
    assert (data_pdf.seen, data_pdf.carried_because) == ("carried", "page_not_reached")
    assert header.gone == 0


def test_a_failed_redirect_link_carries_its_file(site: FakeSite, tmp_path: Path) -> None:
    run_once(site, tmp_path)
    site.routes[f"{SITE}/{GZ}/Anlage.html"] = lambda _: httpx.Response(500)
    later = T0 + timedelta(days=1)
    header = run_once(site, tmp_path, later)
    anlage = by_url(tmp_path, later)[ANLAGE]
    assert (anlage.seen, anlage.carried_because) == ("carried", "page_failed")
    assert (header.status, header.gone) == ("partial", 0)


def test_a_url_that_could_leave_the_root_is_skipped(site: FakeSite, tmp_path: Path) -> None:
    site.decision_links.append(f'<a href="/{GZ}/a%2Fb.pdf?__blob=publicationFile">Seltsam</a>')
    header = run_once(site, tmp_path)
    odd = by_url(tmp_path)[f"{SITE}/{GZ}/a%2Fb.pdf"]
    assert isinstance(odd, SkippedLine) and odd.reason == "unsafe_path"
    assert header.status == "ok"


def test_a_re_check_counts_new_content_under_the_same_v(
    site: FakeSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_once(site, tmp_path)
    old = by_url(tmp_path)[PDF]
    site.routes[f"{PDF}?__blob=publicationFile&v=1"] = lambda _: pdf(b"%PDF quietly replaced")
    monkeypatch.setattr(rulings, "RECHECK_PERIOD", timedelta(days=1))
    later = T0 + timedelta(days=2)
    header = run_once(site, tmp_path, later)
    new = by_url(tmp_path, later)[PDF]
    assert isinstance(old, FileLine) and isinstance(new, FileLine)
    assert header.changed_same_v == 1
    assert new.stored.path != old.stored.path
    assert (tmp_path / old.stored.path).is_file()


def test_a_data_host_file_with_a_new_etag_is_stored_again(site: FakeSite, tmp_path: Path) -> None:
    run_once(site, tmp_path)
    old = by_url(tmp_path)[DATA_PDF]
    site.data_etag, site.data_content = '"neu:0"', b"%PDF gutachten, neu"
    later = T0 + timedelta(hours=1)
    run_once(site, tmp_path, later)
    new = by_url(tmp_path, later)[DATA_PDF]
    assert isinstance(old, FileLine) and isinstance(new, FileLine)
    assert (old.etag, new.etag) == ('"f619a2703fcda1:0"', '"neu:0"')
    assert new.last_modified == "Tue, 31 Oct 2023 21:15:57 GMT"
    assert (tmp_path / new.stored.path).read_bytes() == b"%PDF gutachten, neu"
    assert (tmp_path / old.stored.path).is_file()


def test_a_data_host_file_without_an_etag_gets_only_the_rotation(
    site: FakeSite, tmp_path: Path
) -> None:
    site.data_etag = None
    run_once(site, tmp_path)
    site.forget()
    run_once(site, tmp_path, T0 + timedelta(hours=1))
    assert DATA_PDF not in site.requests
    site.forget()
    run_once(site, tmp_path, T0 + timedelta(days=91))  # a whole period later: everything is due
    assert DATA_PDF in site.requests


def test_leftovers_in_tmp_are_cleared_at_the_start(site: FakeSite, tmp_path: Path) -> None:
    leftover = tmp_path / store.TMP / "half-a-download"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"half")
    run_once(site, tmp_path)
    assert not leftover.exists()


def test_a_new_copy_of_a_page_only_when_main_changed(site: FakeSite, tmp_path: Path) -> None:
    run_once(site, tmp_path)
    old = by_url(tmp_path)[DECISION]
    nav = page(site.decision_main(), nav='<a href="DE/Neu.html">Neu</a>')
    site.routes[DECISION] = lambda _: html(nav)
    later = T0 + timedelta(days=1)
    run_once(site, tmp_path, later)
    same = by_url(tmp_path, later)[DECISION]
    site.decision_links.append('<a href="DE/Vportal/neu.html">Neu</a>')
    site.routes[DECISION] = lambda _: html(page(site.decision_main()))
    latest = later + timedelta(days=1)
    run_once(site, tmp_path, latest)
    changed = by_url(tmp_path, latest)[DECISION]
    assert (
        isinstance(old, PageLine) and isinstance(same, PageLine) and isinstance(changed, PageLine)
    )
    assert same.stored == old.stored
    assert changed.stored is not None and old.stored is not None
    assert changed.stored.path != old.stored.path
    assert (tmp_path / old.stored.path).is_file()


def test_a_broken_run_records_nothing_and_the_next_compares_with_the_last_good(
    site: FakeSite, tmp_path: Path
) -> None:
    run_once(site, tmp_path)
    site.routes[CLOSED] = lambda _: httpx.Response(500)
    broken_at = T0 + timedelta(days=1)
    header = run_once(site, tmp_path, broken_at)
    assert (header.status, header.broken_rule) == ("broken", "list_failed")
    assert header.previous == snapshot.run_id(T0)
    assert lines(tmp_path, broken_at) == []
    site.routes[CLOSED] = FakeSite().routes[CLOSED]
    later = broken_at + timedelta(days=1)
    assert run_once(site, tmp_path, later).previous == snapshot.run_id(T0)


def test_too_few_pages_is_a_broken_run(site: FakeSite, tmp_path: Path) -> None:
    run_once(site, tmp_path)
    site.routes[DECISION] = lambda _: httpx.Response(500)  # the notice is no longer reached
    header = run_once(site, tmp_path, T0 + timedelta(days=1))
    assert (header.status, header.broken_rule) == ("broken", "too_few_pages")
    assert header.reason == "5 pages loaded, the last snapshot has 7 observed or failed"


def test_too_few_rows_is_a_broken_run(site: FakeSite, tmp_path: Path) -> None:
    run_once(site, tmp_path)
    site.routes[ONGOING] = lambda _: html(list_page(""))
    site.routes[CLOSED] = lambda _: html(list_page(""))
    header = run_once(site, tmp_path, T0 + timedelta(days=1))
    assert (header.status, header.broken_rule) == ("broken", "too_few_rows")
    assert header.reason == "0 list rows, the last good run had 3"


def test_the_firewall_page_breaks_the_run(site: FakeSite, tmp_path: Path) -> None:
    site.routes[DECISION] = lambda _: html("The requested URL was rejected. Your support ID is 1")
    header = run_once(site, tmp_path)
    assert header.status == "broken"
    assert header.broken_rule == "firewall"


def test_a_firewall_on_the_v_check_falls_back_to_the_links_v(
    site: FakeSite, tmp_path: Path
) -> None:
    site.routes[PDF] = lambda _: html("The requested URL was rejected.")
    header = run_once(site, tmp_path)
    pdf_line = by_url(tmp_path)[PDF]
    assert isinstance(pdf_line, FileLine)
    assert (header.status, header.v_fallback, pdf_line.v) == ("ok", 1, "1")


def test_files_over_the_size_cap_are_skipped(
    site: FakeSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "MAX_FILE_BYTES", 15)
    run_once(site, tmp_path)
    found = by_url(tmp_path)
    too_large = found[PDF]
    assert isinstance(too_large, SkippedLine) and too_large.reason == "too_large"  # 17 bytes
    assert isinstance(found[ANLAGE], FileLine)  # 11 bytes


def test_skipped_video_is_not_requested_again(site: FakeSite, tmp_path: Path) -> None:
    run_once(site, tmp_path)
    site.forget()
    run_once(site, tmp_path, T0 + timedelta(hours=1))
    assert VIDEO not in site.requests


def test_a_new_file_that_fails_is_recorded_and_tried_again(site: FakeSite, tmp_path: Path) -> None:
    del site.routes[DATA_PDF]
    header = run_once(site, tmp_path)
    failed = by_url(tmp_path)[DATA_PDF]
    assert header.status == "partial"
    assert isinstance(failed, SkippedLine) and failed.reason == "failed"
    site.routes[DATA_PDF] = FakeSite().routes[DATA_PDF]
    later = T0 + timedelta(days=1)
    run_once(site, tmp_path, later)
    assert isinstance(by_url(tmp_path, later)[DATA_PDF], FileLine)


def test_a_crashed_run_is_continued_without_downloading_again(
    site: FakeSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finish = rulings.finish

    def crash(state: rulings.Run, counts: rulings.FileCounts) -> snapshot.RunHeader:
        raise KeyboardInterrupt

    monkeypatch.setattr(rulings, "finish", crash)
    with pytest.raises(KeyboardInterrupt):
        run_once(site, tmp_path)
    assert snapshot.journal_path(tmp_path, snapshot.run_id(T0)).is_file()
    monkeypatch.setattr(rulings, "finish", finish)
    site.forget()
    later = T0 + timedelta(hours=1)
    header = run_once(site, tmp_path, later)
    # The video is asked for its headers again; nothing is downloaded
    assert site.downloads() == Counter({VIDEO: 1})
    assert header.status == "ok" and header.previous is None
    pdf_line = by_url(tmp_path, later)[PDF]
    assert isinstance(pdf_line, FileLine)
    assert (pdf_line.hash, pdf_line.baseline) == ("carried", True)


def test_too_few_pages_leaves_out_pages_no_link_leads_to(
    site: FakeSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_once(site, tmp_path)
    site.decision_links = [link for link in site.decision_links if "Mitteilung" not in link]
    monkeypatch.setattr(rulings, "MIN_RATIO", 0.5)
    later = T0 + timedelta(days=1)
    run_once(site, tmp_path, later)  # the notice is now carried as page_not_reached
    monkeypatch.setattr(rulings, "MIN_RATIO", 0.9)
    # 6 loaded of 6 observed: ok. Counting the unreached notice (6 of 7) would break it.
    assert run_once(site, tmp_path, later + timedelta(days=1)).status == "ok"


def test_a_cut_off_journal_line_is_ignored(site: FakeSite, tmp_path: Path) -> None:
    run_once(site, tmp_path)
    good = by_url(tmp_path)[PDF]
    assert isinstance(good, FileLine)
    crashed = snapshot.journal_path(tmp_path, snapshot.run_id(T0 + timedelta(hours=1)))
    crashed.write_text(good.model_dump_json() + "\n" + good.model_dump_json()[:40])
    # The good run's own journal is ignored; the crashed one is read up to its cut-off line
    assert snapshot.read_journals(tmp_path, snapshot.run_id(T0)) == {PDF: good}
    assert snapshot.read_journals(tmp_path, snapshot.run_id(T0 + timedelta(hours=2))) == {}


@pytest.mark.parametrize(
    ("status", "code"),
    [("partial", 2), ("broken", "rulings: broken run, nothing recorded as gone: list page failed")],
)
def test_main_exits_non_zero_on_a_broken_or_partial_run(
    status: Literal["partial", "broken"], code: int | str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reason = "list page failed" if status == "broken" else None
    header = snapshot.RunHeader(run="r", status=status, reason=reason, previous=None)
    monkeypatch.setattr(fetch, "make_client", httpx.Client)
    monkeypatch.setattr(rulings, "ingest", lambda client: header)
    with pytest.raises(SystemExit) as exit_info:
        rulings.main()
    assert exit_info.value.code == code


def test_main_returns_on_an_ok_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    header = snapshot.RunHeader(run="r", status="ok", previous=None)
    monkeypatch.setattr(fetch, "make_client", httpx.Client)
    monkeypatch.setattr(rulings, "ingest", lambda client: header)
    rulings.main()
    assert capsys.readouterr().out.startswith("rulings: ok;")
