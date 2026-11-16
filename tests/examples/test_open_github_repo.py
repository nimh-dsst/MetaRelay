"""The open_github_repo example, run end to end against the fake GitHub."""

from pathlib import Path

import pytest

from examples.open_github_repo.open_github_repo import main
from examples.save_metadata.save_metadata import main as save_metadata

WHERE = "Fiber study/Cohort A"


@pytest.fixture
def uri(fake_github: str, tmp_path: Path) -> str:
    """The fake's URI, with a record written by the save_metadata example."""
    assert save_metadata([fake_github, "--receipts", str(tmp_path / "receipts.json")]) == 0
    return fake_github


def test_opens_a_folder_and_loads_its_record(uri: str, capsys) -> None:
    capsys.readouterr()
    assert main(["--uri", uri, "--path", WHERE]) == 0
    out = capsys.readouterr().out
    assert f"Opened {WHERE}" in out
    assert "Records: implant-M1" in out
    assert "records/Fiber study/Cohort A/implant-M1.json" in out
    assert "| fiber_diameter_um | 200.0 |" in out


def test_without_a_path_it_lists_the_top_level(uri: str, capsys) -> None:
    capsys.readouterr()
    assert main(["--uri", uri]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Opened the top level")
    assert "  Fiber study/" in out
    assert "No records here" in out


def test_loads_a_record_by_id(uri: str, capsys) -> None:
    assert main(["--uri", uri, "--path", WHERE, "--record", "implant-M1"]) == 0
    assert main(["--uri", uri, "--path", WHERE, "--record", "implant-M2"]) == 1
    assert "No record 'implant-M2'" in capsys.readouterr().err


def test_a_missing_folder_is_a_short_error(uri: str, capsys) -> None:
    assert main(["--uri", uri, "--path", "Nope"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "Traceback" not in err


def test_a_wrong_token_is_a_short_error(uri: str, monkeypatch, capsys) -> None:
    monkeypatch.setenv("METARELAY_GITHUB_TOKEN", "wrong-token")
    assert main(["--uri", uri]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "wrong-token" not in err


def test_runs_as_a_module(uri: str, run_module) -> None:
    result = run_module("examples.open_github_repo", "--uri", uri, "--path", WHERE)
    assert result.returncode == 0, result.stderr
    assert "implant-M1 (lab.implant v1), records/" in result.stdout
