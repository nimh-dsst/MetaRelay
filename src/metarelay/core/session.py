"""The ``Session`` protocol: what every backend implements.

A session is an open, authenticated connection to one destination. Backends
don't inherit from anything. They provide these methods, and
``isinstance(obj, Session)`` checks that they exist.

Every method takes and returns plain data (records, receipts, containers) and
raises only ``metarelay`` errors.

Only ``ensure_container`` creates containers. Every other method raises
``NotFoundError`` for a container that doesn't exist, so a typo in a path is
reported instead of looking like an empty container.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from metarelay.core.conflict import WriteMode
from metarelay.core.model import MetaRelayModel
from metarelay.core.path import ROOT, Container, LogicalPath
from metarelay.core.receipt import FileReceipt, Receipt
from metarelay.core.record import Record

__all__ = ["Session", "StoredRecord"]


class StoredRecord(MetaRelayModel):
    """A record read from a destination, with the receipt that describes it."""

    record: Record
    receipt: Receipt


@runtime_checkable
class Session(Protocol):
    """An open connection to one destination."""

    @property
    def destination(self) -> str:
        """The destination URI this session is connected to."""
        ...

    def list_containers(self, parent: LogicalPath = ROOT) -> list[Container]:
        """List the containers directly inside ``parent``. Not recursive.

        Raises:
            NotFoundError: If ``parent`` does not exist.
        """
        ...

    def ensure_container(self, path: LogicalPath, *, holds_records: bool = False) -> Container:
        """Create ``path`` and its parents if needed, and return it.

        Idempotent: calling it again returns the same container. Afterwards the
        container exists, even while empty: listing it returns ``[]``, not
        ``NotFoundError``. A backend that can't store an empty container
        (Git can't store an empty folder) writes a placeholder to make it real.

        ``holds_records=True`` says the last part of ``path`` will hold records
        and never child containers. Backends with a native "page of entries"
        create one: a LabArchives page or an eLabFTW item. Backends where every
        container is a folder (local, GitHub) ignore it. The hint only affects
        creation: parents are always ordinary containers, and if ``path``
        already exists it is returned as it is, whatever kind it is.

        Raises:
            ValidationError: If a parent in ``path`` is a records-only
                container, on a backend where that is a different kind of
                container (a LabArchives page can't contain anything). Portable
                code never puts a container inside one.
        """
        ...

    def write_record(
        self,
        path: LogicalPath,
        record: Record,
        *,
        mode: WriteMode = WriteMode.UPSERT,
        prior_receipt: Receipt | None = None,
    ) -> Receipt:
        """Write ``record`` and its readable preview into the container at ``path``.

        Uses ``decide_write`` for conflicts, and writes only when the content
        changed. Returns a new receipt to keep for the next write.

        Raises:
            ConflictError: See ``decide_write``.
            NotFoundError: If the container at ``path`` does not exist.
        """
        ...

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        """Read one record.

        Raises:
            NotFoundError: If there is no such record or container.
        """
        ...

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        """Read every record directly inside ``path``. Not recursive.

        Raises:
            NotFoundError: If the container at ``path`` does not exist.
        """
        ...

    def write_file(
        self,
        path: LogicalPath,
        name: str,
        data: bytes,
        *,
        media_type: str | None = None,
    ) -> FileReceipt:
        """Write an arbitrary file (an analysis derivative, for example).

        Backends may cap the size; GitHub does, by design.

        Raises:
            NotFoundError: If the container at ``path`` does not exist.
        """
        ...

    def close(self) -> None:
        """Release the connection. Safe to call twice."""
        ...
