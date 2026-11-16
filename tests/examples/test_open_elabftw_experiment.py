"""The open_elabftw_experiment example, run end to end against the dev stack's eLabFTW."""

from pathlib import Path

import pytest

from examples.open_elabftw_experiment.open_elabftw_experiment import main
from examples.save_metadata.save_metadata import main as save_metadata


@pytest.fixture
def written(elabftw, tmp_path: Path) -> tuple[str, str]:
    """The URI and the path of an entry holding a record written by save_metadata."""
    uri, top = elabftw
    where = f"{top}/Fiber study/Cohort A"
    assert save_metadata([uri, "--path", where, "--receipts", str(tmp_path / "r.json")]) == 0
    return uri, where


def test_opens_an_entry_and_loads_its_record(written, capsys) -> None:
    uri, where = written
    capsys.readouterr()
    assert main(["--uri", uri, "--path", where]) == 0
    out = capsys.readouterr().out
    assert f"Opened {where}" in out
    assert "Records: implant-M1" in out
    assert "| fiber_diameter_um | 200.0 |" in out


def test_lists_the_entries_inside_one(written, capsys) -> None:
    uri, where = written
    top = where.split("/")[0]
    capsys.readouterr()
    assert main(["--uri", uri, "--path", top]) == 0
    out = capsys.readouterr().out
    assert "  Fiber study/" in out
    assert "No records here" in out


def test_without_a_path_it_lists_the_top_level(written, capsys) -> None:
    uri, where = written
    capsys.readouterr()
    assert main(["--uri", uri]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Opened the top level")
    assert f"  {where.split('/')[0]}/" in out


def test_loads_a_record_by_id(written, capsys) -> None:
    uri, where = written
    assert main(["--uri", uri, "--path", where, "--record", "implant-M1"]) == 0
    assert main(["--uri", uri, "--path", where, "--record", "implant-M2"]) == 1
    assert "No record 'implant-M2'" in capsys.readouterr().err


def test_a_missing_entry_is_a_short_error(elabftw, capsys) -> None:
    uri, top = elabftw
    assert main(["--uri", uri, "--path", f"{top}/Nope"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "Traceback" not in err


def test_a_wrong_api_key_is_a_short_error(elabftw, monkeypatch, capsys) -> None:
    uri, _ = elabftw
    monkeypatch.setenv("METARELAY_ELABFTW_TOKEN", "wrong-key")
    assert main(["--uri", uri]) == 1
    err = capsys.readouterr().err
    assert "HTTP 401" in err
    assert "wrong-key" not in err


def test_runs_as_a_module(written, run_module) -> None:
    uri, where = written
    result = run_module("examples.open_elabftw_experiment", "--uri", uri, "--path", where)
    assert result.returncode == 0, result.stderr
    assert "implant-M1 (lab.implant v1), entry " in result.stdout
