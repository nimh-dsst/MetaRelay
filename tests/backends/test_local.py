import json
from pathlib import Path

import pytest

from metarelay import (
    ROOT,
    ConfigurationError,
    ConflictError,
    LogicalPath,
    NotFoundError,
    ValidationError,
    WriteMode,
    connect,
)
from metarelay.backends.local import LocalSession, check_local_name
from metarelay.testing import BackendConformance

STUDY = LogicalPath(("Study",))


class TestLocalBackend(BackendConformance):
    @pytest.fixture
    def session(self, tmp_path: Path):
        session = connect(tmp_path.as_uri())
        yield session
        session.close()

    def edit_preview(self, session, path, record_id, text) -> None:
        session.root.joinpath(*path.parts, f"{record_id}.md").write_text(text)


@pytest.fixture
def session(tmp_path: Path) -> LocalSession:
    session = connect(tmp_path.as_uri())
    session.ensure_container(STUDY)
    return session


def folder(session: LocalSession) -> Path:
    return session.root / "Study"


# Connecting


def test_connect_uses_the_entry_point(tmp_path: Path) -> None:
    session = connect(tmp_path.as_uri() + "/")
    assert isinstance(session, LocalSession)
    assert session.root == tmp_path
    assert session.destination == tmp_path.as_uri()


def test_missing_folder_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        connect((tmp_path / "typo").as_uri())


@pytest.mark.parametrize("uri", ["file://server/share", "file:///tmp?x=1", "file:///tmp#top"])
def test_rejects_hosts_queries_and_fragments(uri: str) -> None:
    with pytest.raises(ConfigurationError):
        connect(uri)


def test_localhost_is_this_computer(tmp_path: Path) -> None:
    assert connect(f"file://localhost{tmp_path.as_posix()}").root == tmp_path


# Layout on disk


def test_record_layout(session: LocalSession, make_record) -> None:
    record = make_record()
    session.write_record(STUDY, record)

    stored = json.loads((folder(session) / "fiber-1.json").read_text())
    assert stored["record_id"] == "fiber-1"
    assert stored["payload"] == {"diameter_um": 400.0}
    assert "| diameter_um | 400.0 |" in (folder(session) / "fiber-1.md").read_text()

    index = json.loads((folder(session) / ".metarelay" / "index.json").read_text())
    assert index == {"index_version": 1, "records": ["fiber-1"], "files": []}


def test_history_has_one_line_per_change(session: LocalSession, make_record) -> None:
    receipt = session.write_record(STUDY, make_record(diameter_um=400))
    session.write_record(STUDY, make_record(diameter_um=400))  # unchanged: no line
    session.write_record(STUDY, make_record(diameter_um=200), prior_receipt=receipt)
    session.write_file(STUDY, "trace.csv", b"1,2\n")

    lines = (folder(session) / ".metarelay" / "history.jsonl").read_text().splitlines()
    history = [json.loads(line) for line in lines]
    assert [(h["kind"], h["name"], h["action"]) for h in history] == [
        ("record", "fiber-1", "create"),
        ("record", "fiber-1", "update"),
        ("file", "trace.csv", "write"),
    ]
    assert history[1]["previous_checksum"] == receipt.checksum


def test_conflict_token_is_the_file_hash(session: LocalSession, make_record) -> None:
    import hashlib

    receipt = session.write_record(STUDY, make_record())
    data = (folder(session) / "fiber-1.json").read_bytes()
    assert receipt.conflict_token == hashlib.sha256(data).hexdigest()


def test_no_temporary_files_are_left(session: LocalSession, make_record) -> None:
    session.write_record(STUDY, make_record())
    with pytest.raises(ConflictError):
        session.write_record(STUDY, make_record(), mode=WriteMode.CREATE)
    assert not [p for p in folder(session).rglob(".tmp-*")]


# Edits made outside MetaRelay


def test_hand_edited_record_conflicts(session: LocalSession, make_record) -> None:
    receipt = session.write_record(STUDY, make_record(diameter_um=400))
    path = folder(session) / "fiber-1.json"
    path.write_text(path.read_text().replace("400.0", "300.0"))

    with pytest.raises(ConflictError):
        session.write_record(STUDY, make_record(diameter_um=200), prior_receipt=receipt)
    assert session.read_record(STUDY, "fiber-1").record.payload == {"diameter_um": 300.0}


def test_reformatting_the_json_is_not_a_change(session: LocalSession, make_record) -> None:
    receipt = session.write_record(STUDY, make_record(diameter_um=400))
    path = folder(session) / "fiber-1.json"
    path.write_text(json.dumps(json.loads(path.read_text())))

    new = session.write_record(STUDY, make_record(diameter_um=200), prior_receipt=receipt)
    assert new.checksum != receipt.checksum


def test_damaged_record_conflicts_and_force_repairs_it(session: LocalSession, make_record) -> None:
    receipt = session.write_record(STUDY, make_record())
    (folder(session) / "fiber-1.json").write_text("{not json")

    with pytest.raises(ValidationError, match=r"fiber-1\.json is not a valid record"):
        session.read_record(STUDY, "fiber-1")
    with pytest.raises(ConflictError):
        session.write_record(STUDY, make_record(), prior_receipt=receipt)
    session.write_record(STUDY, make_record(), mode=WriteMode.FORCE)
    assert session.read_record(STUDY, "fiber-1").record.payload == {"diameter_um": 400.0}


def test_deleted_record_is_written_again(session: LocalSession, make_record) -> None:
    receipt = session.write_record(STUDY, make_record())
    (folder(session) / "fiber-1.json").unlink()
    assert session.list_records(STUDY) == []
    session.write_record(STUDY, make_record(), prior_receipt=receipt)
    assert session.read_record(STUDY, "fiber-1")


def test_missing_preview_is_restored(session: LocalSession, make_record) -> None:
    session.write_record(STUDY, make_record())
    (folder(session) / "fiber-1.md").unlink()
    receipt = session.write_record(STUDY, make_record())
    assert (folder(session) / "fiber-1.md").exists()
    assert receipt.preview_checksum is not None


# Names


@pytest.mark.parametrize(
    "name",
    [
        "a\\b",
        "C:",
        "what?",
        "a*",
        "x|y",
        "<x>",
        'say "hi"',
        ".hidden",
        ".metarelay",
        "ends.",
        "ends ",
        "CON",
        "nul.txt",
        "Com1",
        "x" * 256,
    ],
)
def test_unsafe_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="can't be used on disk"):
        check_local_name(name)


@pytest.mark.parametrize("name", ["Fiber study", "Cohort ä", "M1.v2", "CONSOLE", "notes.json"])
def test_safe_names_are_accepted(name: str) -> None:
    assert check_local_name(name) == name


def test_unsafe_container_is_rejected_before_touching_disk(session: LocalSession) -> None:
    with pytest.raises(ValidationError):
        session.ensure_container(LogicalPath(("ok", "bad:name")))
    assert not (session.root / "ok").exists()


def test_reserved_record_id_is_rejected(session: LocalSession, make_record) -> None:
    with pytest.raises(ValidationError):
        session.write_record(STUDY, make_record("CON"))


def test_metarelay_folder_is_not_a_container(session: LocalSession, make_record) -> None:
    session.write_record(STUDY, make_record())
    assert session.list_containers(STUDY) == []


# Missing containers and files


def test_missing_container_is_not_found(session: LocalSession, make_record) -> None:
    missing = LogicalPath(("Nope",))
    with pytest.raises(NotFoundError):
        session.write_record(missing, make_record())
    with pytest.raises(NotFoundError):
        session.list_records(missing)
    with pytest.raises(NotFoundError):
        session.list_containers(missing)


def test_root_works_as_a_container(session: LocalSession, make_record) -> None:
    record = make_record()
    session.write_record(ROOT, record)
    assert [item.record for item in session.list_records(ROOT)] == [record]


def test_file_cannot_overwrite_a_record(session: LocalSession, make_record) -> None:
    session.write_record(STUDY, make_record())
    for name in ("fiber-1.json", "fiber-1.md"):
        with pytest.raises(ConflictError):
            session.write_file(STUDY, name, b"{}")
    assert session.read_record(STUDY, "fiber-1")


def test_record_cannot_overwrite_a_file(session: LocalSession, make_record) -> None:
    session.write_file(STUDY, "fiber-1.json", b"{}")
    with pytest.raises(ConflictError):
        session.write_record(STUDY, make_record(), mode=WriteMode.FORCE)
    with pytest.raises(NotFoundError):
        session.read_record(STUDY, "fiber-1")
    assert (folder(session) / "fiber-1.json").read_bytes() == b"{}"


def test_damaged_index_says_how_to_fix_it(session: LocalSession, make_record) -> None:
    session.write_record(STUDY, make_record())
    (folder(session) / ".metarelay" / "index.json").write_text("[]")
    with pytest.raises(ValidationError, match="Delete it"):
        session.list_records(STUDY)


def test_deleted_index_is_rebuilt(session: LocalSession, make_record) -> None:
    first, second = make_record("rec-1"), make_record("rec-2")
    session.write_record(STUDY, first)
    session.write_record(STUDY, second)
    (folder(session) / "unrelated.json").write_text("{}")
    (folder(session) / ".metarelay" / "index.json").unlink()

    assert [item.record for item in session.list_records(STUDY)] == [first, second]
    session.write_record(STUDY, make_record("rec-3"))
    index = json.loads((folder(session) / ".metarelay" / "index.json").read_text())
    assert index["records"] == ["rec-1", "rec-2", "rec-3"]


def test_create_is_enforced_when_two_writers_race(
    session: LocalSession, make_record, monkeypatch
) -> None:
    from metarelay.backends import local

    session.write_record(STUDY, make_record(diameter_um=400))
    # The other writer's file appears after our check: pretend we saw nothing.
    with monkeypatch.context() as patch:
        patch.setattr(local, "_read_optional", lambda path: None)
        with pytest.raises(ConflictError):
            session.write_record(STUDY, make_record(diameter_um=200), mode=WriteMode.CREATE)
    assert session.read_record(STUDY, "fiber-1").record.payload == {"diameter_um": 400.0}


def test_record_lost_from_the_index_is_still_listed(session: LocalSession, make_record) -> None:
    session.write_record(STUDY, make_record("rec-1"))
    session.write_record(STUDY, make_record("rec-2"))
    index_file = folder(session) / ".metarelay" / "index.json"
    index = json.loads(index_file.read_text())
    index["records"] = ["rec-1"]  # a racing writer dropped rec-2
    index_file.write_text(json.dumps(index))
    assert [s.record.record_id for s in session.list_records(STUDY)] == ["rec-1", "rec-2"]


@pytest.mark.parametrize(
    "index", ['{"records": "abc", "files": []}', '{"records": [1], "files": []}', "[]"]
)
def test_index_of_the_wrong_shape_is_damaged(session: LocalSession, index: str) -> None:
    (folder(session) / ".metarelay").mkdir()
    (folder(session) / ".metarelay" / "index.json").write_text(index)
    with pytest.raises(ValidationError, match="damaged"):
        session.list_records(STUDY)


def test_unreadable_index_is_permission_denied(session: LocalSession, make_record) -> None:
    import os

    from metarelay import PermissionDeniedError

    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    session.write_record(STUDY, make_record())
    index_file = folder(session) / ".metarelay" / "index.json"
    index_file.chmod(0)
    try:
        with pytest.raises(PermissionDeniedError):
            session.list_records(STUDY)
    finally:
        index_file.chmod(0o644)


def test_other_record_under_a_case_folded_name_conflicts(
    session: LocalSession, make_record
) -> None:
    # What a case-insensitive disk shows: fiber-1.json holds record "Fiber-1".
    session.write_record(STUDY, make_record("Fiber-1"))
    other = folder(session) / "Fiber-1.json"
    target = folder(session) / "fiber-1.json"
    if not target.exists():
        other.rename(target)
    with pytest.raises(ConflictError):
        session.write_record(STUDY, make_record("fiber-1"))
    with pytest.raises(NotFoundError):
        session.read_record(STUDY, "fiber-1")
