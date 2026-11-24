import json
import os
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
    assert main(["write", dest, "Study/Cohort A", record_file(), "--create-path"]) == 0
    capsys.readouterr()

    assert main(["read", dest, "Study/Cohort A", "fiber-1"]) == 0
    assert json.loads(capsys.readouterr().out)["payload"] == {"diameter_um": 400.0}

    assert main(["ls", dest]) == 0
    assert capsys.readouterr().out == "Study/\n"
    assert main(["ls", dest, "Study/Cohort A"]) == 0
    assert capsys.readouterr().out == "fiber-1\ttest.fiber v1\n"


def test_update_without_receipts_is_a_conflict(dest: str, record_file, capsys) -> None:
    main(["mkdir", dest, "Study"])
    main(["write", dest, "Study", record_file(400)])
    assert main(["write", dest, "Study", record_file(200)]) == 3
    assert "--mode force" in capsys.readouterr().err
    assert main(["write", dest, "Study", record_file(200), "--mode", "force"]) == 0


def test_receipts_file_allows_updates(dest: str, record_file, tmp_path: Path) -> None:
    receipts = str(tmp_path / "receipts.json")
    main(["mkdir", dest, "Study"])
    main(["write", dest, "Study", record_file(400), "--receipts", receipts])
    assert main(["write", dest, "Study", record_file(200), "--receipts", receipts]) == 0
    assert len(json.loads(Path(receipts).read_text())["receipts"]) == 1


def test_errors_are_short_messages(dest: str, capsys) -> None:
    assert main(["read", dest, "Study", "fiber-1"]) == 1
    assert capsys.readouterr().err.startswith("metarelay: ")
    assert main(["ls", "nope://x"]) == 1


def test_invalid_record_is_reported(dest: str, tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_id": "test.fiber", "schema_version": 1, "record_id": "x"}')
    main(["mkdir", dest, "Study"])
    assert main(["write", dest, "Study", str(bad)]) == 1
    assert "payload" in capsys.readouterr().err


def test_runs_as_a_module(tmp_path: Path) -> None:
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
    args = ["--import", "lab_schemas", "write", dest, "Notes", str(note), "--create-path"]
    assert main(args) == 0


@pytest.mark.parametrize(
    ("module", "source"),
    [
        ("no_such_module_xyz", None),
        # d75e86d: any failure importing an --import module is a short error.
        ("broken_schemas", "this is not python\n"),
    ],
    ids=["missing-module", "syntax-error"],
)
def test_bad_import_module_is_a_short_error(
    dest: str, tmp_path: Path, monkeypatch, capsys, module: str, source: str | None
) -> None:
    if source is not None:
        (tmp_path / f"{module}.py").write_text(source)
        monkeypatch.syspath_prepend(str(tmp_path))
    assert main(["--import", module, "ls", dest]) == 1
    assert f"Could not import {module!r}" in capsys.readouterr().err


def test_write_to_a_typo_path_creates_nothing(dest: str, record_file, capsys) -> None:
    main(["mkdir", dest, "Fiber study"])
    assert main(["write", dest, "Fiber stdy", record_file()]) == 1
    assert "does not exist" in capsys.readouterr().err
    main(["ls", dest])
    assert capsys.readouterr().out == "Fiber study/\n"


def test_unchanged_write_says_so(dest: str, record_file, capsys) -> None:
    main(["mkdir", dest, "Study"])
    main(["write", dest, "Study", record_file(400)])
    assert capsys.readouterr().err.startswith("Wrote fiber-1")
    main(["write", dest, "Study", record_file(400)])
    assert capsys.readouterr().err.startswith("Already up to date: fiber-1")


def test_receipts_file_leaves_no_temporary_file(dest: str, record_file, tmp_path: Path) -> None:
    main(["mkdir", dest, "Study"])
    main(["write", dest, "Study", record_file(), "--receipts", str(tmp_path / "r.json")])
    assert not list(tmp_path.glob(".r.json*"))


# Review of #18


def test_installed_command_imports_from_the_current_folder(tmp_path: Path) -> None:
    import shutil

    script = shutil.which("metarelay", path=str(Path(sys.executable).parent))
    if script is None:
        pytest.skip("the metarelay console script isn't installed in this environment")
    (tmp_path / "lab_schemas.py").write_text(
        "import pydantic\n"
        "from metarelay import register_schema\n"
        "class Note(pydantic.BaseModel):\n"
        "    model_config = pydantic.ConfigDict(extra='forbid')\n"
        "    text: str\n"
        "register_schema('lab.note', 1, Note)\n"
    )
    (tmp_path / "dest").mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [script, "--import", "lab_schemas", "ls", (tmp_path / "dest").as_uri()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_conflict_advice_fits_the_command(dest: str, record_file, tmp_path: Path, capsys) -> None:
    main(["mkdir", dest, "Study"])
    main(["write", dest, "Study", record_file(400)])
    capsys.readouterr()

    assert main(["write", dest, "Study", record_file(400), "--mode", "create"]) == 3
    err = capsys.readouterr().err
    assert "--receipts" not in err
    assert "--mode force" in err

    receipts = str(tmp_path / "r.json")
    main(["read", dest, "Study", "fiber-1", "--receipts", receipts])
    (Path(dest.removeprefix("file://")) / "Study" / "fiber-1.md").write_text("edited\n")
    capsys.readouterr()
    assert main(["write", dest, "Study", record_file(200), "--receipts", receipts]) == 3
    assert "--receipts" not in capsys.readouterr().err


def test_mkdir_under_a_file_is_a_plain_error(dest: str, capsys) -> None:
    main(["mkdir", dest, "Study"])
    (Path(dest.removeprefix("file://")) / "Study" / "trace.csv").write_text("1")
    assert main(["mkdir", dest, "Study/trace.csv/sub"]) == 1
    err = capsys.readouterr().err
    assert "'Study/trace.csv' is a file, not a folder" in err
    assert "--mode" not in err and "--receipts" not in err


def test_force_over_an_edited_preview_says_wrote(dest: str, record_file, capsys) -> None:
    main(["mkdir", dest, "Study"])
    main(["write", dest, "Study", record_file(400)])
    preview = Path(dest.removeprefix("file://")) / "Study" / "fiber-1.md"
    preview.write_text(preview.read_text() + "Lab note: fiber cracked\n")
    capsys.readouterr()
    assert main(["write", dest, "Study", record_file(400), "--mode", "force"]) == 0
    assert capsys.readouterr().err.startswith("Wrote fiber-1")
    assert "cracked" not in preview.read_text()


def test_unsavable_receipts_fail_before_writing(dest: str, record_file, tmp_path: Path) -> None:
    main(["mkdir", dest, "Study"])
    bad = str(tmp_path / "nodir" / "r.json")
    assert main(["write", dest, "Study", record_file(), "--receipts", bad]) == 1
    assert not (Path(dest.removeprefix("file://")) / "Study" / "fiber-1.json").exists()


def test_holds_records_flag_reaches_the_session(record_file, monkeypatch, capsys) -> None:
    # MemorySession refuses a container inside a records-only one, like a
    # LabArchives page does, so the refusal shows the flag was passed on.
    from metarelay.testing import MemorySession

    session = MemorySession()
    monkeypatch.setattr("metarelay.cli.connect", lambda uri: session)
    dest = "memory://test"

    assert main(["mkdir", dest, "Study/M1", "--holds-records"]) == 0
    assert main(["mkdir", dest, "Study/M1/Session 1"]) == 1
    assert "holds records only" in capsys.readouterr().err

    assert main(["write", dest, "Study/M2", record_file(), "--create-path", "--holds-records"]) == 0
    assert main(["mkdir", dest, "Study/M2/Session 1"]) == 1

    assert main(["mkdir", dest, "Study/M3"]) == 0
    assert main(["mkdir", dest, "Study/M3/Session 1"]) == 0


def test_holds_records_without_create_path_is_refused(dest: str, record_file, capsys) -> None:
    # Without --create-path, write never creates a container, so the flag would do nothing.
    main(["mkdir", dest, "Study"])
    with pytest.raises(SystemExit) as exit_info:
        main(["write", dest, "Study", record_file(), "--holds-records"])
    assert exit_info.value.code == 2
    assert "--holds-records only applies with --create-path" in capsys.readouterr().err
    assert main(["ls", dest, "Study"]) == 0
    assert capsys.readouterr().out == ""


# Edits made in the notebook


def _edit_table(dest: str, path: str, record_id: str, old: str, new: str) -> None:
    md = Path(dest.removeprefix("file://")).joinpath(*path.split("/"), f"{record_id}.md")
    md.write_text(md.read_text().replace(old, new))


def test_a_write_lists_notebook_edits_and_can_accept_them(
    dest: str, record_file, tmp_path: Path, capsys
) -> None:
    receipts = str(tmp_path / "receipts.json")

    def write(file: str, *extra: str) -> int:
        return main(["write", dest, "Study", file, "--receipts", receipts, *extra])

    main(["mkdir", dest, "Study"])
    assert write(record_file(400)) == 0
    _edit_table(dest, "Study", "fiber-1", "| diameter_um | 400.0 |", "| diameter_um | 250 |")
    capsys.readouterr()

    # Someone made the notebook say 250; this file says 300. Both changed the
    # same value, so the edit can't simply be accepted, and it isn't offered.
    update = record_file(300)
    assert write(update) == 3
    err = capsys.readouterr().err
    assert "Changed: Metadata · diameter_um: '400.0' → '250'" in err
    assert "--accept-edits" not in err
    assert write(update, "--accept-edits") == 1
    assert "no longer shows '400.0'" in capsys.readouterr().err

    # The same record as before: the edit can be accepted.
    same = record_file(400)
    assert write(same) == 3
    assert "re-run with --accept-edits" in capsys.readouterr().err
    assert write(same, "--accept-edits") == 0
    err = capsys.readouterr().err
    assert "Accepted from the preview: fiber-1: Metadata · diameter_um: '400.0' → '250'" in err
    assert main(["read", dest, "Study", "fiber-1"]) == 0
    assert json.loads(capsys.readouterr().out)["payload"] == {"diameter_um": 250.0}
    # The file has the edit too, so writing it again keeps it.
    assert json.loads(Path(same).read_text())["payload"] == {"diameter_um": 250.0}
    assert write(same) == 0
    assert main(["read", dest, "Study", "fiber-1"]) == 0
    assert json.loads(capsys.readouterr().out)["payload"] == {"diameter_um": 250.0}


def test_accept_edits_from_standard_input_prints_the_edited_record(
    dest: str, record_file, tmp_path: Path, capsys, monkeypatch
) -> None:
    import io

    receipts = str(tmp_path / "receipts.json")
    main(["mkdir", dest, "Study"])
    assert main(["write", dest, "Study", record_file(400), "--receipts", receipts]) == 0
    _edit_table(dest, "Study", "fiber-1", "| 400.0 |", "| 250 |")
    capsys.readouterr()
    data = Path(record_file(400)).read_bytes()
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(data)))
    assert main(["write", dest, "Study", "-", "--receipts", receipts, "--accept-edits"]) == 0
    assert json.loads(capsys.readouterr().out)["payload"] == {"diameter_um": 250.0}


def test_accept_edits_refuses_changes_it_cant_list(
    dest: str, record_file, tmp_path, capsys
) -> None:
    receipts = str(tmp_path / "receipts.json")
    main(["mkdir", dest, "Study"])
    assert main(["write", dest, "Study", record_file(), "--receipts", receipts]) == 0
    _edit_table(dest, "Study", "fiber-1", "| Value |", "| Value |\nThe fiber broke.")
    capsys.readouterr()
    args = ["write", dest, "Study", record_file(), "--receipts", receipts, "--accept-edits"]
    assert main(args) == 3
    err = capsys.readouterr().err
    assert "MetaRelay can't list" in err
    assert "--mode force" in err


def test_accept_edits_is_for_upsert_only(dest: str, record_file) -> None:
    with pytest.raises(SystemExit):
        main(["write", dest, "Study", record_file(), "--mode", "force", "--accept-edits"])
