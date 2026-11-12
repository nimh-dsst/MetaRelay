"""The conformance suite: the checks every backend must pass.

A backend is done when it passes this suite. To run it against a backend,
subclass ``BackendConformance`` in a test module and provide a ``session``
fixture that returns a fresh, empty session::

    import pytest
    from metarelay.testing import BackendConformance

    class TestMyBackend(BackendConformance):
        @pytest.fixture
        def session(self, tmp_path):
            session = connect(f"mybackend://...")
            yield session
            session.close()

If the backend can simulate a person editing the readable preview directly
(D7), also override ``edit_preview``. Otherwise those checks are skipped.

If records-only containers (``holds_records=True``) can't contain other
containers on the backend, set ``records_only_is_a_leaf = True``.

The suite registers its own small schema, ``metarelay.conformance`` v1, in
the default registry, so it does not depend on any real schema.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from collections.abc import Callable

import pydantic
import pytest

from metarelay.core import registry as registry_module
from metarelay.core.checksum import record_checksum
from metarelay.core.conflict import WriteMode
from metarelay.core.destination import Destination
from metarelay.core.errors import ConflictError, NotFoundError, ValidationError
from metarelay.core.path import ROOT, Container, LogicalPath
from metarelay.core.receipt import FileReceipt, Receipt
from metarelay.core.record import Record
from metarelay.core.session import Session, StoredRecord

__all__ = ["CONFORMANCE_SCHEMA_ID", "BackendConformance", "ConformancePayload"]

CONFORMANCE_SCHEMA_ID = "metarelay.conformance"


class ConformancePayload(pydantic.BaseModel):
    """The payload of every record the suite writes."""

    model_config = pydantic.ConfigDict(extra="forbid")

    label: str
    value: float | None = None


RecordFactory = Callable[..., Record]


class BackendConformance:
    """Mix into a ``Test*`` class and provide a ``session`` fixture."""

    records_only_is_a_leaf: bool = False
    """True if ``ensure_container(path, holds_records=True)`` makes a container
    that can't contain other containers (a LabArchives page). The suite then
    checks that putting one inside raises ``ValidationError``; otherwise it
    checks that it works like any other container."""

    @pytest.fixture
    def session(self) -> Session:
        raise NotImplementedError("Override the session fixture to return a fresh session.")

    def edit_preview(self, session: Session, path: LogicalPath, record_id: str, text: str) -> None:
        """Replace a record's preview as a person editing the entry would.

        Optional. Backends without previews, or that can't simulate an edit,
        leave this alone and the preview-edit checks are skipped.
        """
        raise NotImplementedError

    @pytest.fixture(autouse=True)
    def _conformance_schema(self) -> None:
        # Look the registry up at call time so a test that swapped it is respected.
        registry_module.default_registry.register(CONFORMANCE_SCHEMA_ID, 1, ConformancePayload)

    @pytest.fixture
    def make_record(self) -> RecordFactory:
        def _make(record_id: str = "rec-1", label: str = "first", **payload: object) -> Record:
            return Record(
                schema_id=CONFORMANCE_SCHEMA_ID,
                schema_version=1,
                record_id=record_id,
                context={"cohort": "A", "subject_id": "M1"},
                payload={"label": label, **payload},
            )

        return _make

    @pytest.fixture
    def container(self, session: Session) -> LogicalPath:
        """A fresh container, unique per test so live backends don't collide."""
        path = LogicalPath(("MetaRelay conformance", uuid.uuid4().hex[:12]))
        session.ensure_container(path)
        return path

    # The protocol

    def test_is_a_session(self, session: Session) -> None:
        assert isinstance(session, Session)

    def test_destination_is_normalized(self, session: Session) -> None:
        assert Destination.parse(session.destination).uri == session.destination

    def test_close_twice_is_safe(self, session: Session) -> None:
        session.close()
        session.close()

    # Containers

    def test_ensure_container_is_idempotent(self, session: Session) -> None:
        path = LogicalPath(("Study",))
        first = session.ensure_container(path)
        second = session.ensure_container(path)
        assert first.path == path
        assert second == first

    def test_ensure_container_creates_parents(self, session: Session) -> None:
        session.ensure_container(LogicalPath(("Study", "Cohort A", "M1")))
        assert _paths(session.list_containers(ROOT)) >= {LogicalPath(("Study",))}
        assert _paths(session.list_containers(LogicalPath(("Study",)))) == {
            LogicalPath(("Study", "Cohort A"))
        }

    def test_list_containers_is_not_recursive(self, session: Session) -> None:
        session.ensure_container(LogicalPath(("Study", "Cohort A")))
        session.ensure_container(LogicalPath(("Study", "Cohort B")))
        listed = _paths(session.list_containers(LogicalPath(("Study",))))
        assert listed == {LogicalPath(("Study", "Cohort A")), LogicalPath(("Study", "Cohort B"))}

    def test_ensured_empty_container_lists_as_empty(self, session: Session) -> None:
        path = LogicalPath(("Empty",))
        session.ensure_container(path)
        assert session.list_records(path) == []
        assert session.list_containers(path) == []
        assert path in _paths(session.list_containers(ROOT))

    def test_missing_container_is_not_found(self, session: Session, make_record) -> None:
        # Only ensure_container creates containers, so a typo is reported.
        missing = LogicalPath(("Missing",))
        with pytest.raises(NotFoundError):
            session.list_containers(missing)
        with pytest.raises(NotFoundError):
            session.list_records(missing)
        with pytest.raises(NotFoundError):
            session.read_record(missing, "rec-1")
        with pytest.raises(NotFoundError):
            session.write_record(missing, make_record())
        with pytest.raises(NotFoundError):
            session.write_file(missing, "trace.csv", b"data")
        assert missing not in _paths(session.list_containers(ROOT))

    # Records-only containers (ensure_container's holds_records hint)

    def test_records_only_container_is_idempotent(self, session: Session) -> None:
        path = LogicalPath(("Study", "M1"))
        first = session.ensure_container(path, holds_records=True)
        second = session.ensure_container(path, holds_records=True)
        assert first.path == path
        assert second == first
        assert _paths(session.list_containers(LogicalPath(("Study",)))) == {path}
        assert session.list_records(path) == []

    def test_records_only_container_holds_records(self, session: Session, make_record) -> None:
        path = LogicalPath(("Study", "M1"))
        session.ensure_container(path, holds_records=True)
        first = make_record("rec-1")
        session.write_record(path, first)
        session.write_record(path, make_record("rec-2"))
        assert session.read_record(path, "rec-1").record == first
        assert {item.record.record_id for item in session.list_records(path)} == {"rec-1", "rec-2"}

    def test_records_only_hint_leaves_parents_ordinary(self, session: Session) -> None:
        session.ensure_container(LogicalPath(("Study", "Cohort A", "M1")), holds_records=True)
        sibling = LogicalPath(("Study", "Cohort A", "M2"))
        session.ensure_container(sibling)
        assert sibling in _paths(session.list_containers(LogicalPath(("Study", "Cohort A"))))

    def test_records_only_hint_does_not_change_an_existing_container(
        self, session: Session
    ) -> None:
        folder = LogicalPath(("Study",))
        created = session.ensure_container(folder)
        assert session.ensure_container(folder, holds_records=True) == created
        session.ensure_container(folder / "Cohort A")  # still an ordinary container

        page = LogicalPath(("Other", "M1"))
        created = session.ensure_container(page, holds_records=True)
        assert session.ensure_container(page) == created

    def test_container_inside_records_only_container(self, session: Session, make_record) -> None:
        path = LogicalPath(("Study", "M1"))
        session.ensure_container(path, holds_records=True)
        record = make_record()
        session.write_record(path, record)
        child = path / "Session 1"
        if not self.records_only_is_a_leaf:
            session.ensure_container(child)
            assert _paths(session.list_containers(path)) == {child}
            return
        with pytest.raises(ValidationError):
            session.ensure_container(child)
        with pytest.raises(ValidationError):
            session.ensure_container(child / "deeper", holds_records=True)
        # Nothing half-created, and the records are untouched.
        assert session.list_containers(path) == []
        assert [item.record for item in session.list_records(path)] == [record]

    def test_names_with_spaces_and_unicode(self, session: Session, make_record) -> None:
        path = LogicalPath(("Fiber study", "Cohort ä"))
        session.ensure_container(path)
        record = make_record()
        session.write_record(path, record)
        assert session.read_record(path, "rec-1").record == record

    # Records

    def test_write_then_read(self, session: Session, container: LogicalPath, make_record) -> None:
        record = make_record(value=1.5)
        receipt = session.write_record(container, record)
        stored = session.read_record(container, record.record_id)

        assert stored.record == record
        assert stored.receipt.checksum == record_checksum(record) == receipt.checksum
        assert stored.receipt.conflict_token == receipt.conflict_token

    def test_receipt_describes_the_write(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        record = make_record()
        receipt = session.write_record(container, record)

        assert receipt.destination == session.destination
        assert receipt.container.path == container
        assert receipt.record_id == record.record_id
        assert receipt.schema_id == record.schema_id
        assert receipt.schema_version == record.schema_version
        assert receipt.conflict_token
        assert Receipt.model_validate_json(receipt.model_dump_json()) == receipt

    def test_read_missing_record(self, session: Session, container: LogicalPath) -> None:
        with pytest.raises(NotFoundError):
            session.read_record(container, "missing")

    def test_list_records(self, session: Session, container: LogicalPath, make_record) -> None:
        session.write_record(container, make_record("rec-1"))
        session.write_record(container, make_record("rec-2"))
        child = container / "child"
        session.ensure_container(child)
        in_child = make_record("rec-3")
        session.write_record(child, in_child)

        listed = session.list_records(container)
        assert all(isinstance(item, StoredRecord) for item in listed)
        assert {item.record.record_id for item in listed} == {"rec-1", "rec-2"}
        assert [item.record for item in session.list_records(child)] == [in_child]

    def test_empty_container_has_no_records(self, session: Session, container) -> None:
        assert session.list_records(container) == []

    def test_same_id_in_two_containers(self, session: Session, make_record) -> None:
        first, second = LogicalPath(("A",)), LogicalPath(("B",))
        session.ensure_container(first)
        session.ensure_container(second)
        session.write_record(first, make_record(label="in A"))
        session.write_record(second, make_record(label="in B"))
        assert session.read_record(first, "rec-1").record.payload["label"] == "in A"
        assert session.read_record(second, "rec-1").record.payload["label"] == "in B"

    # Write modes and conflicts

    def test_upsert_of_identical_content_needs_no_receipt(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        first = session.write_record(container, make_record())
        again = session.write_record(container, make_record())
        assert again.checksum == first.checksum

    def test_upsert_with_receipt_updates(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        receipt = session.write_record(container, make_record(label="old"))
        new = session.write_record(container, make_record(label="new"), prior_receipt=receipt)
        assert session.read_record(container, "rec-1").record.payload["label"] == "new"
        assert new.checksum != receipt.checksum

    def test_receipt_from_a_read_allows_an_update(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        session.write_record(container, make_record(label="old"))
        receipt = session.read_record(container, "rec-1").receipt
        session.write_record(container, make_record(label="new"), prior_receipt=receipt)
        assert session.read_record(container, "rec-1").record.payload["label"] == "new"

    def test_upsert_without_receipt_conflicts_with_different_record(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        session.write_record(container, make_record(label="theirs"))
        with pytest.raises(ConflictError):
            session.write_record(container, make_record(label="mine"))
        assert session.read_record(container, "rec-1").record.payload["label"] == "theirs"

    def test_upsert_conflicts_when_remote_changed_since_receipt(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        receipt = session.write_record(container, make_record(label="v1"))
        # Someone else changes the record at the destination.
        session.write_record(container, make_record(label="theirs"), mode=WriteMode.FORCE)
        with pytest.raises(ConflictError):
            session.write_record(container, make_record(label="mine"), prior_receipt=receipt)
        assert session.read_record(container, "rec-1").record.payload["label"] == "theirs"

    def test_create_writes_a_new_record(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        record = make_record()
        session.write_record(container, record, mode=WriteMode.CREATE)
        assert session.read_record(container, "rec-1").record == record

    def test_create_refuses_an_existing_record(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        receipt = session.write_record(container, make_record(label="first"))
        for record in (make_record(label="first"), make_record(label="second")):
            with pytest.raises(ConflictError):
                session.write_record(
                    container, record, mode=WriteMode.CREATE, prior_receipt=receipt
                )
        assert session.read_record(container, "rec-1").record.payload["label"] == "first"

    def test_force_overwrites(self, session: Session, container: LogicalPath, make_record) -> None:
        session.write_record(container, make_record(label="theirs"))
        receipt = session.write_record(container, make_record(label="mine"), mode=WriteMode.FORCE)
        assert session.read_record(container, "rec-1").record.payload["label"] == "mine"
        assert receipt.checksum == record_checksum(make_record(label="mine"))

    def test_write_mode_accepts_strings(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        session.write_record(container, make_record(label="theirs"))
        session.write_record(container, make_record(label="mine"), mode="force")  # type: ignore[arg-type]
        assert session.read_record(container, "rec-1").record.payload["label"] == "mine"

    # Previews (D7)

    def test_preview_edit_conflicts(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        receipt = session.write_record(container, make_record(label="v1"))
        if receipt.preview_checksum is None:
            pytest.skip("This backend does not track previews.")
        try:
            self.edit_preview(session, container, "rec-1", "Edited by hand\n")
        except NotImplementedError:
            pytest.skip("This backend cannot simulate a preview edit.")
        with pytest.raises(ConflictError):
            session.write_record(container, make_record(label="v2"), prior_receipt=receipt)
        session.write_record(container, make_record(label="v2"), mode=WriteMode.FORCE)
        assert session.read_record(container, "rec-1").record.payload["label"] == "v2"

    def test_preview_edit_conflicts_even_after_a_read(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        # Reading returns the JSON, not the preview, so the caller hasn't seen
        # the edit: the read's receipt must not accept it (D7).
        receipt = session.write_record(container, make_record(label="v1"))
        if receipt.preview_checksum is None:
            pytest.skip("This backend does not track previews.")
        try:
            self.edit_preview(session, container, "rec-1", "Edited by hand\n")
        except NotImplementedError:
            pytest.skip("This backend cannot simulate a preview edit.")
        from_read = session.read_record(container, "rec-1").receipt
        [listed] = session.list_records(container)
        assert from_read.preview_checksum == receipt.preview_checksum
        assert listed.receipt.preview_checksum == receipt.preview_checksum
        with pytest.raises(ConflictError):
            session.write_record(container, make_record(label="v2"), prior_receipt=from_read)
        # force replaces the edit, and the next read reflects the new preview.
        forced = session.write_record(container, make_record(label="v2"), mode=WriteMode.FORCE)
        assert session.read_record(container, "rec-1").receipt.preview_checksum == (
            forced.preview_checksum
        )

    # Files

    def test_write_file(self, session: Session, container: LogicalPath) -> None:
        data = b"time,signal\n0,1.5\n"
        receipt = session.write_file(container, "trace.csv", data, media_type="text/csv")

        assert isinstance(receipt, FileReceipt)
        assert receipt.destination == session.destination
        assert receipt.container.path == container
        assert receipt.name == "trace.csv"
        assert receipt.checksum == hashlib.sha256(data).hexdigest()
        assert receipt.size == len(data)

    def test_write_file_twice_replaces_it(self, session: Session, container: LogicalPath) -> None:
        session.write_file(container, "trace.csv", b"old")
        receipt = session.write_file(container, "trace.csv", b"new")
        assert receipt.checksum == hashlib.sha256(b"new").hexdigest()

    @pytest.mark.parametrize("name", ["rec-1.json", "rec-1.md", "REC-1.json"])
    def test_write_file_never_replaces_a_record(
        self, session: Session, container: LogicalPath, make_record, name: str
    ) -> None:
        # A backend may refuse the name, or keep files apart from records,
        # but the record must survive either way.
        record = make_record()
        session.write_record(container, record)
        with contextlib.suppress(ConflictError, ValidationError):
            session.write_file(container, name, b"{}")
        assert session.read_record(container, "rec-1").record == record
        assert [item.record for item in session.list_records(container)] == [record]

    def test_ids_that_differ_only_in_case_never_replace_each_other(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        # Refusing Rec-1 or storing it separately are both fine; losing rec-1 isn't.
        record = make_record("rec-1", label="lower")
        session.write_record(container, record)
        with contextlib.suppress(ConflictError, ValidationError):
            session.write_record(container, make_record("Rec-1", label="upper"), mode="force")
        assert session.read_record(container, "rec-1").record == record

    def test_unknown_write_mode_is_a_validation_error(
        self, session: Session, container: LogicalPath, make_record
    ) -> None:
        with pytest.raises(ValidationError):
            session.write_record(container, make_record(), mode="Force")  # type: ignore[arg-type]

    def test_files_are_not_records(self, session: Session, container: LogicalPath) -> None:
        session.write_file(container, "notes.json", b"{}")
        assert session.list_records(container) == []


def _paths(containers: list[Container]) -> set[LogicalPath]:
    return {container.path for container in containers}
