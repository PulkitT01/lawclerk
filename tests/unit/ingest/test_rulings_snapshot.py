from pathlib import Path

import pytest
from conftest import (
    CLOSED,
    T0,
)

from lawclerk.ingest.rulings import snapshot
from lawclerk.ingest.rulings.snapshot import RowLine

pytestmark = pytest.mark.usefixtures("no_backoff")


def test_read_snapshot_refuses_an_unknown_format(tmp_path: Path) -> None:
    header = snapshot.RunHeader(run="r", status="ok", previous=None, format=99)
    path = tmp_path / "s.jsonl"
    path.write_text(header.model_dump_json() + "\n")
    with pytest.raises(ValueError, match="format 99"):
        snapshot.read_snapshot(path)


def test_snapshot_round_trip(tmp_path: Path) -> None:
    header = snapshot.RunHeader(run=snapshot.run_id(T0), status="ok", previous=None)
    row = RowLine(
        seen="observed",
        list_page=CLOSED,
        aktenzeichen="BK6-1",
        cells=["BK6-1", "", "", ""],
        links=[],
        first_seen="r",
        baseline=True,
    )
    path = snapshot.write_snapshot(tmp_path, snapshot.Snapshot(header, [row]))
    assert path.name == "2026-11-16T09-00-00Z.jsonl"
    assert snapshot.read_snapshot(path).lines == [row]
    with pytest.raises(FileExistsError):
        snapshot.write_snapshot(tmp_path, snapshot.Snapshot(header, []))
