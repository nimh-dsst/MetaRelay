"""Write modes and the conflict rules every backend shares.

The rules are lifted from Chronoxiv's ``_upsert_record``. They live here as a
pure function so every backend makes the same decision and the rules can be
tested without a backend::

    action = decide_write(
        mode=mode,
        local_checksum=record_checksum(record),
        remote_checksum=checksum_of_what_is_there_now,   # None if nothing is
        prior_receipt=prior_receipt,
    )
    if action is WriteAction.WRITE:
        ...  # upload the record
"""

from __future__ import annotations

from enum import StrEnum

from metarelay.core.errors import ConflictError, ValidationError
from metarelay.core.receipt import Receipt

__all__ = ["WriteAction", "WriteMode", "decide_write"]


class WriteMode(StrEnum):
    """How ``write_record`` treats a record that already exists."""

    CREATE = "create"
    """Fail if a record with this ID exists.

    Best effort: LabArchives and eLabFTW can only check and then write, so two
    writers at the same moment can both succeed. Local (exclusive file
    create) and GitHub (PUT without a sha) can enforce it.
    """
    UPSERT = "upsert"
    """The default. Create or update, raising ``ConflictError`` if the remote
    record changed in a way MetaRelay did not make."""
    FORCE = "force"
    """Overwrite whatever is there. Use only after the user chose to."""


class WriteAction(StrEnum):
    """What the backend should do after ``decide_write`` returns."""

    WRITE = "write"
    SKIP = "skip"
    """The destination already holds this exact content."""


def decide_write(
    *,
    mode: WriteMode,
    local_checksum: str,
    remote_checksum: str | None,
    prior_receipt: Receipt | None,
    remote_preview_checksum: str | None = None,
) -> WriteAction:
    """Decide whether to write, skip, or refuse.

    Args:
        mode: The write mode the caller asked for, as a ``WriteMode`` or its
            string value (``"create"``).
        local_checksum: ``record_checksum`` of the record being written.
        remote_checksum: ``record_checksum`` of the record at the destination
            now, or ``None`` if there is none.
        prior_receipt: The receipt from the last write or read, if any.
        remote_preview_checksum: ``preview_checksum`` of the preview at the
            destination now, for backends that track previews (D7).

    A record that was deleted at the destination since the last sync is
    written again, as in Chronoxiv.

    Raises:
        ConflictError: If writing would overwrite changes MetaRelay did not make.
        ValidationError: If ``mode`` is not a write mode.
    """
    # Accept "create" as well as WriteMode.CREATE: modes often arrive as text
    # (a CLI flag, a config file). The checks below compare with ``is``, so a
    # plain string would match nothing and quietly act like upsert.
    try:
        mode = WriteMode(mode)
    except ValueError as error:
        modes = ", ".join(m.value for m in WriteMode)
        raise ValidationError(f"Unknown write mode {mode!r}; expected one of {modes}.") from error

    if remote_checksum is None:
        return WriteAction.WRITE

    if mode is WriteMode.FORCE:
        return WriteAction.WRITE

    if mode is WriteMode.CREATE:
        raise ConflictError("A record with this ID already exists at the destination.")

    if prior_receipt is None:
        if remote_checksum != local_checksum:
            raise ConflictError("A different record already uses this ID at the destination.")
    elif remote_checksum not in {prior_receipt.checksum, local_checksum}:
        raise ConflictError("The record changed at the destination since it was last synced.")

    if (
        prior_receipt is not None
        and prior_receipt.preview_checksum is not None
        and remote_preview_checksum is not None
        and remote_preview_checksum != prior_receipt.preview_checksum
    ):
        raise ConflictError(
            "The notebook entry was edited directly since it was last synced. To keep "
            "the edit, copy it into the record first. Then write again with "
            "mode='force' to replace the entry."
        )

    return WriteAction.SKIP if remote_checksum == local_checksum else WriteAction.WRITE
