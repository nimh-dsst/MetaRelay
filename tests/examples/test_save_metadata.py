"""The save_metadata example, run end to end against a folder and the fake LabArchives."""

import json
from pathlib import Path

import pytest

from examples.save_metadata.save_metadata import main

WHERE = Path("Fiber study") / "Cohort A"


def run(folder: Path, receipts: Path, *extra: str) -> int:
    return main([folder.as_uri(), "--receipts", str(receipts), *extra])


def test_writes_reads_and_updates_a_record_in_a_folder(tmp_path: Path, capsys) -> None:
    folder, receipts = tmp_path / "metadata", tmp_path / "receipts.json"
    folder.mkdir()

    assert run(folder, receipts) == 0

    stored = json.loads((folder / WHERE / "implant-M1.json").read_text())
    assert stored["payload"] == {"fiber_diameter_um": 200.0, "target_region": "VTA"}
    assert (folder / WHERE / "implant-M1.md").exists()
    assert len(json.loads(receipts.read_text())["receipts"]) == 1
    out = capsys.readouterr().out
    assert "Read it back: {'fiber_diameter_um': 400.0" in out
    assert "Updated it:   {'fiber_diameter_um': 200.0" in out


def test_a_second_run_uses_the_saved_receipts(tmp_path: Path) -> None:
    folder, receipts = tmp_path / "metadata", tmp_path / "receipts.json"
    folder.mkdir()
    assert run(folder, receipts) == 0
    assert run(folder, receipts) == 0


def test_a_second_run_without_receipts_is_a_conflict(tmp_path: Path) -> None:
    # Without the receipt, MetaRelay can't tell its own earlier write from
    # someone else's, so it refuses.
    folder = tmp_path / "metadata"
    folder.mkdir()
    assert run(folder, tmp_path / "first.json") == 0
    assert run(folder, tmp_path / "second.json") == 3


def test_a_change_at_the_destination_stops_the_run_until_forced(tmp_path: Path, capsys) -> None:
    folder, receipts = tmp_path / "metadata", tmp_path / "receipts.json"
    folder.mkdir()
    assert run(folder, receipts) == 0
    record_file = folder / WHERE / "implant-M1.json"
    record_file.write_text(record_file.read_text().replace("200.0", "300.0"))
    capsys.readouterr()

    assert run(folder, receipts) == 3
    err = capsys.readouterr().err
    assert "'fiber_diameter_um': 300.0" in err
    assert "--force" in err
    assert "300.0" in record_file.read_text()  # not overwritten

    assert run(folder, receipts, "--force") == 0
    assert "200.0" in record_file.read_text()


def test_a_missing_folder_is_a_short_error(tmp_path: Path, capsys) -> None:
    assert run(tmp_path / "nope", tmp_path / "receipts.json") == 1
    assert "does not exist" in capsys.readouterr().err


def test_runs_as_a_module(run_module, tmp_path: Path) -> None:
    folder = tmp_path / "metadata"
    folder.mkdir()
    result = run_module(
        "examples.save_metadata", folder.as_uri(), "--receipts", str(tmp_path / "r.json")
    )
    assert result.returncode == 0, result.stderr
    assert "Updated it" in result.stdout


def test_writes_to_labarchives_with_the_same_code(fake_labarchives, tmp_path: Path) -> None:
    pytest.importorskip("labapi")
    fake, uri = fake_labarchives
    path = "Test notebook/Fiber study/Cohort A"
    receipts = tmp_path / "receipts.json"
    args = [uri, "--path", path, "--receipts", str(receipts), "--auto-login"]

    assert main(args) == 0
    assert main(args) == 0  # the receipts carry over between runs
    files = [entry.filename for entry in fake._entries.values()]
    assert files.count("implant-M1.json") == 1  # updated in place, not added twice
