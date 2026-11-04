"""The ``Session`` protocol: what every backend implements.

A session is an open, authenticated connection to one destination. Backends
don't inherit from anything. They provide these methods, and
``isinstance(obj, Session)`` checks that they exist.

Every method takes and returns plain data (records, receipts, containers) and
raises only ``metarelay`` errors.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pydantic
from pydantic import ConfigDict

from metarelay.core.conflict import WriteMode
from metarelay.core.path import ROOT, Container, LogicalPath
from metarelay.core.receipt import FileReceipt, Receipt
from metarelay.core.record import Record

__all__ = ["Session", "StoredRecord"]


class StoredRecord(pydantic.BaseModel):
    """A record read from a destination, with the receipt that describes it."""

    model_config = ConfigDict(frozen=True)

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
        """List the containers directly inside ``parent``. Not recursive."""
        ...

    def ensure_container(self, path: LogicalPath) -> Container:
        """Make ``path`` ready to write to, and return it.

        Idempotent: calling it again returns the same container. "Ready to
        write to" does not promise an empty container exists. Git cannot store
        empty directories, so the GitHub backend may create it on first write.
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
        """
        ...

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        """Read one record.

        Raises:
            NotFoundError: If there is no such record.
        """
        ...

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        """Read every record directly inside ``path``. Not recursive."""
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
        """
        ...

    def close(self) -> None:
        """Release the connection. Safe to call twice."""
        ...
