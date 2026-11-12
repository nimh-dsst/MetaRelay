"""The Session protocol, checked against a throwaway dict-backed session.

The official in-memory fake and the conformance suite arrive in #4. This one
only proves the protocol is implementable and that ``decide_write`` slots in.
"""

import hashlib

import pytest

from metarelay import (
    ROOT,
    ConflictError,
    Container,
    FileReceipt,
    LogicalPath,
    NotFoundError,
    Receipt,
    Record,
    Session,
    StoredRecord,
    WriteAction,
    WriteMode,
    record_checksum,
)
from metarelay.core.conflict import decide_write


class DictSession:
    def __init__(self) -> None:
        self.containers: set[LogicalPath] = set()
        self.records: dict[tuple[LogicalPath, str], Record] = {}
        self.closed = False

    @property
    def destination(self) -> str:
        return "memory://test"

    def list_containers(self, parent: LogicalPath = ROOT) -> list[Container]:
        return [Container(path=p) for p in sorted(self.containers, key=str) if p.parent == parent]

    def ensure_container(self, path: LogicalPath, *, holds_records: bool = False) -> Container:
        self.containers.add(path)
        return Container(path=path)

    def _receipt(self, path: LogicalPath, record: Record) -> Receipt:
        checksum = record_checksum(record)
        return Receipt(
            destination=self.destination,
            backend="memory",
            container=Container(path=path),
            record_id=record.record_id,
            schema_id=record.schema_id,
            schema_version=record.schema_version,
            checksum=checksum,
            conflict_token=checksum,
        )

    def write_record(
        self,
        path: LogicalPath,
        record: Record,
        *,
        mode: WriteMode = WriteMode.UPSERT,
        prior_receipt: Receipt | None = None,
    ) -> Receipt:
        remote = self.records.get((path, record.record_id))
        action = decide_write(
            mode=mode,
            local_checksum=record_checksum(record),
            remote_checksum=record_checksum(remote) if remote else None,
            prior_receipt=prior_receipt,
        )
        if action is WriteAction.WRITE:
            self.records[(path, record.record_id)] = record
        return self._receipt(path, record)

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        record = self.records.get((path, record_id))
        if record is None:
            raise NotFoundError(f"No record {record_id!r} in {path}.")
        return StoredRecord(record=record, receipt=self._receipt(path, record))

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        return [self.read_record(p, rid) for (p, rid) in self.records if p == path]

    def write_file(
        self, path: LogicalPath, name: str, data: bytes, *, media_type: str | None = None
    ) -> FileReceipt:
        return FileReceipt(
            destination=self.destination,
            backend="memory",
            container=Container(path=path),
            name=name,
            checksum=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def session() -> DictSession:
    return DictSession()


def test_a_plain_class_satisfies_the_protocol(session: DictSession) -> None:
    assert isinstance(session, Session)


def test_an_incomplete_class_does_not() -> None:
    class Partial:
        def close(self) -> None: ...

    assert not isinstance(Partial(), Session)


def test_ensure_container_is_idempotent(session: DictSession) -> None:
    path = ROOT / "Study"
    assert session.ensure_container(path) == session.ensure_container(path)
    assert session.list_containers() == [Container(path=path)]


def test_write_then_read(session: DictSession, make_record) -> None:
    path = ROOT / "Study"
    record = make_record()
    receipt = session.write_record(path, record)
    stored = session.read_record(path, record.record_id)
    assert stored.record == record
    assert stored.receipt.checksum == receipt.checksum
    assert [item.record for item in session.list_records(path)] == [record]


def test_update_with_receipt_then_conflict_without_one(session: DictSession, make_record) -> None:
    path = ROOT / "Study"
    receipt = session.write_record(path, make_record(diameter_um=400))
    session.write_record(path, make_record(diameter_um=200), prior_receipt=receipt)
    assert session.read_record(path, "fiber-1").record.payload == {"diameter_um": 200}
    with pytest.raises(ConflictError):
        session.write_record(path, make_record(diameter_um=100))


def test_missing_record_is_not_found(session: DictSession) -> None:
    with pytest.raises(NotFoundError):
        session.read_record(ROOT, "missing")


def test_stored_record_is_plain_data(session: DictSession, make_record) -> None:
    session.write_record(ROOT, make_record())
    stored = session.read_record(ROOT, "fiber-1")
    assert StoredRecord.model_validate_json(stored.model_dump_json()) == stored
