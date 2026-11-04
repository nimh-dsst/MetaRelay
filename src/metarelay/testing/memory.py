"""An in-memory backend for tests.

``MemorySession`` keeps everything in dicts and behaves like a real backend:
it uses ``decide_write`` for conflicts, enforces ``create``, stores a
Markdown preview with every record, and raises only ``metarelay`` errors.
Use it to test code that calls MetaRelay without touching disk or a network::

    from metarelay.testing import MemorySession

    session = MemorySession()
    session.ensure_container(ROOT / "Study")
    receipt = session.write_record(ROOT / "Study", record)

It passes the same conformance suite as every real backend.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from metarelay.core.auth import AuthFlow, LoginChallenge
from metarelay.core.checksum import preview_checksum, record_checksum
from metarelay.core.config import DestinationConfig
from metarelay.core.conflict import WriteAction, WriteMode, decide_write
from metarelay.core.destination import Destination
from metarelay.core.errors import ConfigurationError, NotFoundError
from metarelay.core.path import ROOT, Container, LogicalPath
from metarelay.core.preview import render_preview
from metarelay.core.receipt import FileReceipt, Receipt
from metarelay.core.record import Record
from metarelay.core.session import StoredRecord

__all__ = ["MemoryBackend", "MemorySession"]

BACKEND_NAME = "memory"


@dataclass
class _Stored:
    record: Record
    preview: str


class MemorySession:
    """A ``Session`` that keeps records in memory. Nothing outlives the object."""

    def __init__(self, destination: str = "memory://test") -> None:
        self._destination = Destination.parse(destination).uri
        self._containers: set[LogicalPath] = {ROOT}
        self._records: dict[tuple[LogicalPath, str], _Stored] = {}
        self.files: dict[tuple[LogicalPath, str], bytes] = {}
        """Files written with ``write_file``, for tests to inspect."""
        self.closed = False

    @property
    def destination(self) -> str:
        return self._destination

    def list_containers(self, parent: LogicalPath = ROOT) -> list[Container]:
        self._require_container(parent)
        children = [path for path in self._containers if path != ROOT and path.parent == parent]
        return [Container(path=path) for path in sorted(children, key=lambda p: p.parts)]

    def ensure_container(self, path: LogicalPath) -> Container:
        for depth in range(1, len(path.parts) + 1):
            self._containers.add(LogicalPath(path.parts[:depth]))
        return Container(path=path)

    def write_record(
        self,
        path: LogicalPath,
        record: Record,
        *,
        mode: WriteMode = WriteMode.UPSERT,
        prior_receipt: Receipt | None = None,
    ) -> Receipt:
        self._require_container(path)
        key = (path, record.record_id)
        remote = self._records.get(key)
        action = decide_write(
            mode=mode,  # decide_write rejects an unknown mode with ValidationError
            local_checksum=record_checksum(record),
            remote_checksum=record_checksum(remote.record) if remote else None,
            prior_receipt=prior_receipt,
            remote_preview_checksum=preview_checksum(remote.preview) if remote else None,
        )
        if action is WriteAction.WRITE:
            self._records[key] = _Stored(record, render_preview(record).markdown)
        return self._receipt(path, self._records[key])

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        stored = self._records.get((path, record_id))
        if stored is None:
            raise NotFoundError(f"No record {record_id!r} in {str(path) or 'the root'}.")
        return StoredRecord(record=stored.record, receipt=self._receipt(path, stored))

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        self._require_container(path)
        ids = sorted(record_id for (where, record_id) in self._records if where == path)
        return [self.read_record(path, record_id) for record_id in ids]

    def write_file(
        self,
        path: LogicalPath,
        name: str,
        data: bytes,
        *,
        media_type: str | None = None,
    ) -> FileReceipt:
        self._require_container(path)
        self.files[(path, name)] = bytes(data)
        return FileReceipt(
            destination=self.destination,
            backend=BACKEND_NAME,
            container=Container(path=path),
            name=name,
            checksum=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )

    def close(self) -> None:
        self.closed = True

    # Test hooks: changes made "outside MetaRelay".

    def preview_of(self, path: LogicalPath, record_id: str) -> str:
        """The stored Markdown preview of a record."""
        return self._get(path, record_id).preview

    def edit_preview(self, path: LogicalPath, record_id: str, text: str) -> None:
        """Replace a record's preview, as a person editing the entry would."""
        self._get(path, record_id).preview = text

    def _get(self, path: LogicalPath, record_id: str) -> _Stored:
        stored = self._records.get((path, record_id))
        if stored is None:
            raise NotFoundError(f"No record {record_id!r} in {str(path) or 'the root'}.")
        return stored

    def _require_container(self, path: LogicalPath) -> None:
        if path not in self._containers:
            raise NotFoundError(f"Container {str(path)!r} does not exist. Create it first.")

    def _receipt(self, path: LogicalPath, stored: _Stored) -> Receipt:
        checksum = record_checksum(stored.record)
        return Receipt(
            destination=self.destination,
            backend=BACKEND_NAME,
            container=Container(path=path),
            record_id=stored.record.record_id,
            schema_id=stored.record.schema_id,
            schema_version=stored.record.schema_version,
            checksum=checksum,
            conflict_token=checksum,
            preview_checksum=preview_checksum(stored.preview),
        )


class MemoryBackend:
    """A ``Backend`` that opens ``MemorySession`` objects.

    Not registered as an entry point, so ``connect("memory://...")`` does not
    work in normal use. Tests can install it by monkeypatching
    ``metarelay.core.backend.load_backend``. Every ``open`` of the same URI
    returns the same session, so data survives between ``connect`` calls
    within one backend object.
    """

    name = BACKEND_NAME
    auth_flow = AuthFlow.NONE

    def __init__(self) -> None:
        self.sessions: dict[str, MemorySession] = {}

    def open(self, destination: Destination, config: DestinationConfig | None) -> MemorySession:
        if destination.uri not in self.sessions:
            self.sessions[destination.uri] = MemorySession(destination.uri)
        return self.sessions[destination.uri]

    def begin_login(
        self, destination: Destination, redirect_url: str, config: DestinationConfig | None
    ) -> LoginChallenge:
        raise ConfigurationError("memory:// does not use a browser login.")

    def complete_login(
        self,
        challenge: LoginChallenge,
        params: Mapping[str, str],
        config: DestinationConfig | None,
    ) -> MemorySession:
        raise ConfigurationError("memory:// does not use a browser login.")
