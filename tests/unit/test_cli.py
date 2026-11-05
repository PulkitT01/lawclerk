import pytest

from lawclerk import cli


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the ingesters with fakes that record which kinds were run."""
    calls: list[str] = []
    fakes = {
        "statutes": lambda: calls.append("statutes"),
        "rulings": lambda: calls.append("rulings"),
    }
    monkeypatch.setattr(cli, "INGESTERS", fakes)
    return calls


def test_ingest_with_kind_runs_only_that_kind(calls: list[str]) -> None:
    cli.main(["ingest", "rulings"])
    assert calls == ["rulings"]


def test_ingest_without_kind_runs_every_kind(calls: list[str]) -> None:
    cli.main(["ingest"])
    assert calls == ["statutes", "rulings"]


def test_ingest_rejects_unknown_kind(calls: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["ingest", "contracts"])
    assert exit_info.value.code == 2
    assert "invalid choice: 'contracts'" in capsys.readouterr().err
    assert calls == []


def test_command_is_required(calls: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2
    assert calls == []


def test_statutes_is_registered_with_the_statutes_ingester() -> None:
    from lawclerk.ingest import statutes

    assert cli.INGESTERS["statutes"] is statutes.main
