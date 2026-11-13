"""Write a metadata record to any destination, read it back, and update it.

From the repository root, with no account::

    mkdir -p metadata
    python -m examples.save_metadata "file://$PWD/metadata"

The same code works for every backend; only the destination URI changes.
``README.md`` in this folder covers LabArchives and what to do when the
record changed at the destination (a ``ConflictError``).

The code has two layers, as in ``labapi``'s examples:

- ``save_metadata`` and ``write`` take an open session and return receipts.
  Import them to reuse the steps.
- ``main`` is the command line: arguments, login, output, and exit codes.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import examples.lab_schemas  # noqa: F401  registers lab.implant
from examples.login import Browser, ask_in_terminal, follow_fake_login, open_session
from metarelay import (
    ConflictError,
    LogicalPath,
    MetaRelayError,
    Receipt,
    Record,
    Session,
    SyncIndex,
    WriteMode,
)

RECORD_ID = "implant-M1"
CONFLICT_EXIT = 3
"""The exit status for a conflict, the same as the ``metarelay`` command's."""


@dataclass(frozen=True)
class Saved:
    """What ``save_metadata`` did."""

    written: Receipt
    read_back: Record
    updated: Receipt
    update: Record


# [docs:write-with-receipt]
def write(
    session: Session,
    where: LogicalPath,
    record: Record,
    receipts: SyncIndex,
    *,
    force: bool = False,
) -> Receipt:
    """Write ``record`` with the receipt from the last write, and keep the new one.

    Raises:
        ConflictError: If the record changed at the destination since then,
            unless ``force`` is set.
    """
    prior = receipts.get(session.destination, where, record.record_id)
    try:
        receipt = session.write_record(where, record, prior_receipt=prior)
    except ConflictError:
        # Someone changed the record at the destination since `prior`.
        # Stop, unless the person already chose to replace their change.
        if not force:
            raise
        receipt = session.write_record(where, record, mode=WriteMode.FORCE)
    receipts.put(receipt)
    return receipt


# [/docs:write-with-receipt]


def save_metadata(
    session: Session, where: LogicalPath, receipts: SyncIndex, *, force: bool = False
) -> Saved:
    """Write the implant record into ``where``, read it back, and update it."""
    # [docs:write-and-read]
    session.ensure_container(where)

    record = Record(
        schema_id="lab.implant",
        schema_version=1,
        record_id="implant-M1",
        context={"cohort": "A", "subject_id": "M1"},
        payload={"fiber_diameter_um": 400, "target_region": "VTA"},
    )
    written = write(session, where, record, receipts, force=force)

    stored = session.read_record(where, "implant-M1")
    # stored.record.payload == {"fiber_diameter_um": 400.0, "target_region": "VTA"}
    # [/docs:write-and-read]

    # [docs:update]
    changed = Record.model_validate(
        {**record.model_dump(), "payload": {"fiber_diameter_um": 200, "target_region": "VTA"}}
    )
    updated = write(session, where, changed, receipts)
    # [/docs:update]
    return Saved(written=written, read_back=stored.record, updated=updated, update=changed)


def load_receipts(path: Path) -> SyncIndex:
    """The receipts saved by the last run, or none on the first run."""
    if not path.exists():
        return SyncIndex()
    return SyncIndex.model_validate_json(path.read_text())


def main(argv: Sequence[str] | None = None, *, browser: Browser | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m examples.save_metadata",
        description="Write a metadata record to a destination, read it back, and update it.",
    )
    parser.add_argument("destination", help="a destination URI, such as file:///path/to/folder")
    parser.add_argument(
        "--path",
        default="Fiber study/Cohort A",
        help="where the record goes, names separated by '/'. For LabArchives, the "
        "first name is an existing notebook (default: %(default)s)",
    )
    parser.add_argument(
        "--receipts",
        type=Path,
        default=Path("receipts.json"),
        help="where receipts are kept between runs (default: %(default)s)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace the record even if it changed at the destination",
    )
    parser.add_argument(
        "--auto-login",
        action="store_true",
        help="log in to the fake LabArchives server without a browser",
    )
    args = parser.parse_args(argv)
    if browser is None:
        browser = follow_fake_login if args.auto_login else ask_in_terminal

    try:
        where = LogicalPath.parse(args.path)
        receipts = load_receipts(args.receipts)
        session = open_session(args.destination, browser=browser)
    except MetaRelayError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    try:
        saved = save_metadata(session, where, receipts, force=args.force)
    except ConflictError:
        explain_conflict(session, where)
        return CONFLICT_EXIT
    except MetaRelayError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        session.close()
        # Saved even after a failure: receipts for the writes that worked still count.
        args.receipts.write_text(receipts.model_dump_json(indent=2) + "\n")

    print(f"Wrote {RECORD_ID} to {saved.written.destination} in {where}")
    print(f"Read it back: {saved.read_back.payload}")
    print(f"Updated it:   {saved.update.payload}")
    print(f"Receipts saved in {args.receipts}")
    return 0


def explain_conflict(session: Session, where: LogicalPath) -> None:
    """Say what changed and how to go on. MetaRelay never picks for the person."""
    try:
        theirs = session.read_record(where, RECORD_ID).record.payload
    except MetaRelayError as error:
        theirs = f"(could not read it: {error})"
    print(
        f"{RECORD_ID} changed at {session.destination} since this script last wrote it, "
        "so MetaRelay did not overwrite it.\n"
        f"  At the destination now: {theirs}\n"
        "To replace it with this script's record, run again with --force.\n"
        "This script always writes the same record, so it can't keep that change. "
        "To keep one in your own code, see docs/concepts.md#handling-a-conflict.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    sys.exit(main())
