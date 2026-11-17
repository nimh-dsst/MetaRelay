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

from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING

from metarelay.core.errors import ConflictError, ValidationError
from metarelay.core.preview import PreviewDiff, PreviewEdit, apply_edits, best_effort
from metarelay.core.receipt import Receipt

if TYPE_CHECKING:
    from metarelay.core.path import LogicalPath
    from metarelay.core.record import Record
    from metarelay.core.registry import SchemaRegistry
    from metarelay.core.session import Session, StoredRecord

__all__ = ["WriteAction", "WriteMode", "accept_edits", "decide_write"]


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
    preview_diff: Callable[[], PreviewDiff | None] | None = None,
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
        preview_diff: Says what changed in an edited preview (usually a
            ``preview.preview_diff`` call). Called only when the preview was
            edited; its result goes on the ``ConflictError``.

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
        diff = best_effort(preview_diff) if preview_diff is not None else None
        raise ConflictError(
            # Every backend with previews comes here (a .md file on disk or
            # GitHub, a LabArchives entry), so name what they share.
            "The record's preview was edited directly since it was last synced. "
            + (diff.describe() + " " if diff is not None else "")
            + "To keep the edit, copy it into the record first. Then write again "
            "with mode='force' to replace the preview.",
            preview_diff=diff,
        )

    return WriteAction.SKIP if remote_checksum == local_checksum else WriteAction.WRITE


def accept_edits(
    session: Session,
    path: LogicalPath,
    record: Record,
    diff: PreviewDiff,
    *,
    registry: SchemaRegistry | None = None,
) -> dict[str, StoredRecord]:
    """Keep the notebook edits a person accepted: copy them into the records and write them.

    ``record`` is the one whose write raised the ``ConflictError`` that
    carried ``diff``. On eLabFTW one entry holds several records, the diff
    lists edits to all of them, and a forced write rebuilds the whole entry,
    so the other records are read from the destination, get their edits
    too, and are written as well; otherwise their edits would be lost. Every
    edit is applied (``apply_edits``) before anything is written.

    The writes are forced, so a change someone makes at the destination
    while this runs is overwritten, like any forced write.

    Returns every record written, with its edits, and a receipt read after
    the last write; ``record`` comes first.

    Raises:
        ConflictError: If ``diff`` is incomplete: someone also changed
            something MetaRelay can't list, so nothing is written.
        ValidationError: If an edit can't be applied; nothing is written.
    """
    if not diff.complete:
        raise ConflictError(
            "Someone also changed something in the entry that MetaRelay can't list, so "
            "the edits can't be accepted automatically. Look at the entry, copy what you "
            "want into the record, and write it with mode='force'.",
            preview_diff=diff,
        )
    by_record: dict[str, list[PreviewEdit]] = {record.record_id: []}
    for edit in diff.edits:
        by_record.setdefault(edit.record_id, []).append(edit)
    updated = {}
    for record_id, edits in by_record.items():
        base = (
            record if record_id == record.record_id else session.read_record(path, record_id).record
        )
        updated[record_id] = apply_edits(base, edits, registry=registry)
    # The other records first: each forced write rebuilds an eLabFTW entry,
    # and the last one, ``record``'s, is the one the caller asked for.
    for record_id in reversed(list(updated)):
        session.write_record(path, updated[record_id], mode=WriteMode.FORCE)
    # Read back, so every receipt describes the destination after the last write.
    return {record_id: session.read_record(path, record_id) for record_id in updated}
