"""The open_labarchives_notebook example, run end to end against the fake LabArchives."""

from pathlib import Path

import pytest

pytest.importorskip("labapi")

from examples.open_labarchives_notebook.open_labarchives_notebook import main
from examples.save_metadata.save_metadata import main as save_metadata

NOTEBOOK, WHERE = "Test notebook", "Fiber study/Cohort A"


@pytest.fixture
def uri(fake_labarchives, tmp_path: Path) -> str:
    """The fake's URI, with a record written by the save_metadata example."""
    _, uri = fake_labarchives
    receipts = str(tmp_path / "receipts.json")
    args = [uri, "--path", f"{NOTEBOOK}/{WHERE}", "--receipts", receipts, "--auto-login"]
    assert save_metadata(args) == 0
    return uri


def test_logs_in_opens_a_notebook_and_loads_a_record(uri: str, capsys) -> None:
    capsys.readouterr()
    args = ["--uri", uri, "--notebook", NOTEBOOK, "--path", WHERE, "--auto-login"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "Notebooks: Test notebook, Lab Notebook" in out
    assert "Records: implant-M1" in out
    assert "| fiber_diameter_um | 200.0 |" in out


def test_loads_a_record_by_id(uri: str, capsys) -> None:
    args = ["--uri", uri, "--notebook", NOTEBOOK, "--path", WHERE, "--auto-login"]
    assert main([*args, "--record", "implant-M1"]) == 0
    assert main([*args, "--record", "implant-M2"]) == 1
    assert "No record 'implant-M2'" in capsys.readouterr().err


def test_without_a_notebook_it_lists_them(uri: str, capsys) -> None:
    capsys.readouterr()
    assert main(["--uri", uri, "--auto-login"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Test notebook", "Lab Notebook"]


def test_lists_folders_and_pages_where_there_are_no_records(uri: str, capsys) -> None:
    args = ["--uri", uri, "--notebook", "Lab Notebook", "--path", "Example experiment"]
    assert main([*args, "--auto-login"]) == 0
    out = capsys.readouterr().out
    assert "  Notes (page)" in out
    assert "No records here" in out


def test_an_unknown_notebook_names_the_real_ones(uri: str, capsys) -> None:
    assert main(["--uri", uri, "--notebook", "Lab notebok", "--auto-login"]) == 1
    assert "Yours: Test notebook, Lab Notebook" in capsys.readouterr().err


def test_without_auto_login_it_reads_the_pasted_address(uri: str, monkeypatch, capsys) -> None:
    # The path a real account takes: print the link, read back the address
    # LabArchives sent the browser to. The seed's demo user logs in with "demo".
    pasted = "http://localhost:8089/callback?email=demo%40example.org&auth_code=demo"
    monkeypatch.setattr("builtins.input", lambda prompt="": pasted)
    assert main(["--uri", uri, "--notebook", NOTEBOOK, "--path", WHERE]) == 0
    out = capsys.readouterr().out
    assert "Open this link and log in" in out
    assert "Records: implant-M1" in out


def test_a_rejected_login_is_a_short_error(fake_labarchives, monkeypatch, capsys) -> None:
    _, uri = fake_labarchives
    monkeypatch.setenv("METARELAY_LABARCHIVES_AKPASS", "wrong")
    assert main(["--uri", uri, "--notebook", NOTEBOOK, "--auto-login"]) == 1
    err = capsys.readouterr().err
    assert "Check METARELAY_LABARCHIVES_AKID" in err
    assert "sig=" not in err


def test_an_unreachable_server_is_a_short_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv("METARELAY_LABARCHIVES_AKID", "id")
    monkeypatch.setenv("METARELAY_LABARCHIVES_AKPASS", "secret")
    monkeypatch.setenv("METARELAY_LABARCHIVES_API_URL", "http://127.0.0.1:9")
    assert main(["--uri", "labarchives://127.0.0.1:9", "--auto-login"]) == 1
    err = capsys.readouterr().err
    assert "Could not reach the login page" in err
    assert "sig=" not in err


def test_runs_as_a_module(uri: str, run_module) -> None:
    result = run_module(
        "examples.open_labarchives_notebook",
        *["--uri", uri, "--notebook", NOTEBOOK, "--path", WHERE, "--auto-login"],
    )
    assert result.returncode == 0, result.stderr
    assert "implant-M1 (lab.implant v1)" in result.stdout
