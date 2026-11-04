import json
import subprocess
import sys
from pathlib import Path

import pytest

from metarelay.cli import main


@pytest.fixture
def dest(tmp_path: Path) -> str:
    root = tmp_path / "dest"
    root.mkdir()
    return root.as_uri()


@pytest.fixture
def record_file(tmp_path: Path):
    def _write(diameter_um: float = 400.0, record_id: str = "fiber-1") -> str:
        path = tmp_path / f"{record_id}-{diameter_um}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_id": "test.fiber",
                    "schema_version": 1,
                    "record_id": record_id,
                    "payload": {"diameter_um": diameter_um},
                }
            )
        )
        return str(path)

    return _write


def test_write_read_and_list(dest: str, record_file, capsys) -> None:
    assert main(["write", dest, "Study/Cohort A", record_file()]) == 0
    capsys.readouterr()

    assert main(["read", dest, "Study/Cohort A", "fiber-1"]) == 0
    assert json.loads(capsys.readouterr().out)["payload"] == {"diameter_um": 400.0}

    assert main(["ls", dest]) == 0
    assert capsys.readouterr().out == "Study/\n"
    assert main(["ls", dest, "Study/Cohort A"]) == 0
    assert capsys.readouterr().out == "fiber-1\ttest.fiber v1\n"


def test_mkdir(dest: str, capsys) -> None:
    assert main(["mkdir", dest, "Study/Cohort A"]) == 0
    main(["ls", dest, "Study"])
    assert capsys.readouterr().out == "Cohort A/\n"


def test_update_without_receipts_is_a_conflict(dest: str, record_file, capsys) -> None:
    main(["write", dest, "Study", record_file(400)])
    assert main(["write", dest, "Study", record_file(200)]) == 3
    assert "--mode force" in capsys.readouterr().err
    assert main(["write", dest, "Study", record_file(200), "--mode", "force"]) == 0


def test_receipts_file_allows_updates(dest: str, record_file, tmp_path: Path) -> None:
    receipts = str(tmp_path / "receipts.json")
    main(["write", dest, "Study", record_file(400), "--receipts", receipts])
    assert main(["write", dest, "Study", record_file(200), "--receipts", receipts]) == 0
    assert len(json.loads(Path(receipts).read_text())["receipts"]) == 1


def test_errors_are_short_messages(dest: str, capsys) -> None:
    assert main(["read", dest, "Study", "fiber-1"]) == 1
    assert capsys.readouterr().err.startswith("metarelay: ")
    assert main(["ls", "nope://x"]) == 1
    assert main(["--import", "no_such_module_xyz", "ls", dest]) == 1


def test_invalid_record_is_reported(dest: str, tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_id": "test.fiber", "schema_version": 1, "record_id": "x"}')
    assert main(["write", dest, "Study", str(bad)]) == 1
    assert "payload" in capsys.readouterr().err


def test_installed_command_runs(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "metarelay.cli", "ls", tmp_path.as_uri()],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_import_registers_schemas(dest: str, tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "lab_schemas.py").write_text(
        "import pydantic\n"
        "from metarelay import register_schema\n"
        "class Note(pydantic.BaseModel):\n"
        "    model_config = pydantic.ConfigDict(extra='forbid')\n"
        "    text: str\n"
        "register_schema('lab.note', 1, Note)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    note = tmp_path / "note.json"
    note.write_text(
        '{"schema_id": "lab.note", "schema_version": 1, "record_id": "n1",'
        ' "payload": {"text": "hi"}}'
    )
    assert main(["--import", "lab_schemas", "write", dest, "Notes", str(note)]) == 0
