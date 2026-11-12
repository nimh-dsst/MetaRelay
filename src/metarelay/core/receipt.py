"""Receipts: proof of a write, used to detect conflicts on the next one.

Lifted from Chronoxiv's ``SyncReceipt`` and ``LabArchivesSyncIndex``. The
LabArchives-only fields (``page_id``, ``attachment_entry_id``,
``preview_entry_id``) move into ``backend_ids``, and the notebook and
experiment IDs become a backend-neutral ``Container``.

A receipt holds two different fingerprints:

- ``checksum``: MetaRelay's fingerprint of the record content
  (``record_checksum``), the same on every backend.
- ``conflict_token``: the backend's own version marker, for example GitHub's
  blob ``sha``. The core stores it and hands it back; only the backend reads it.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

import pydantic
from pydantic import ConfigDict, Field, field_validator

from metarelay.core.destination import normalize_destination, validate_destination
from metarelay.core.model import MetaRelayModel, TranslatesValidationErrors
from metarelay.core.path import Container, LogicalPath

__all__ = ["FileReceipt", "Receipt", "SyncIndex", "receipt_key"]


def _now() -> datetime:
    return datetime.now().astimezone()


def receipt_key(destination: str, path: LogicalPath, record_id: str) -> str:
    """The key a receipt is stored under in a ``SyncIndex``.

    Includes the destination so the same record can be synced to several
    destinations, each with its own receipt. Encoded as a JSON list so a
    ``|`` or ``/`` inside a name can't make two different keys collide.

    The destination is normalized, so ``LabArchives://host/`` finds the
    receipt saved for ``labarchives://host``.
    """
    return json.dumps(
        [normalize_destination(destination), list(path.parts), record_id], ensure_ascii=False
    )


class Receipt(MetaRelayModel):
    """What MetaRelay knows about a record after writing or reading it."""

    destination: str
    """The destination URI the record was written to, normalized."""
    backend: str
    """The backend name, for example ``"labarchives"``."""
    container: Container
    record_id: str
    schema_id: str
    schema_version: int
    checksum: str
    """``record_checksum`` of the record as it is at the destination."""
    conflict_token: str
    """The backend's own version marker. Opaque to the core."""
    preview_checksum: str | None = None
    """Fingerprint of the preview as MetaRelay last wrote it, to detect edits
    made directly in the notebook (D7, pending DSST confirmation). A receipt
    from a read carries the same value, not a fingerprint of the preview as it
    is now: reading returns the JSON, so the caller hasn't seen an edit to the
    preview, and the next write must still report it. ``None`` for receipts
    made before previews were tracked."""
    backend_ids: dict[str, str] = Field(default_factory=dict)
    """Backend-specific IDs, for example LabArchives' ``page_id`` and
    ``attachment_entry_id``."""
    synced_at: datetime = Field(default_factory=_now)

    _normalize_destination = field_validator("destination")(validate_destination)

    @property
    def key(self) -> str:
        return receipt_key(self.destination, self.container.path, self.record_id)


class FileReceipt(MetaRelayModel):
    """Proof of a ``write_file`` call (an analysis derivative, for example)."""

    destination: str
    backend: str
    container: Container
    name: str
    checksum: str
    """SHA-256 of the file bytes."""
    size: int
    native_id: str | None = None
    synced_at: datetime = Field(default_factory=_now)

    _normalize_destination = field_validator("destination")(validate_destination)


class SyncIndex(TranslatesValidationErrors, pydantic.BaseModel):
    """Every receipt an application holds, saved with its workspace.

    Plain data: the application decides where to store it (Chronoxiv keeps it
    next to the workspace as JSON).
    """

    model_config = ConfigDict(extra="forbid")

    index_version: Literal[1] = 1
    receipts: dict[str, Receipt] = Field(default_factory=dict)

    def get(self, destination: str, path: LogicalPath, record_id: str) -> Receipt | None:
        return self.receipts.get(receipt_key(destination, path, record_id))

    def put(self, receipt: Receipt) -> None:
        self.receipts[receipt.key] = receipt

    def remove(self, destination: str, path: LogicalPath, record_id: str) -> None:
        self.receipts.pop(receipt_key(destination, path, record_id), None)
