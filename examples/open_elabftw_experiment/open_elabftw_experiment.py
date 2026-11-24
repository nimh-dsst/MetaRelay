"""Connect to eLabFTW with an API key, list what is there, open an entry, and load a record.

Needs ``pip install 'metarelay[elabftw]'``. Against the dev stack's eLabFTW,
with no account (from the repository root)::

    docker compose up -d --wait elabftw
    docker compose exec elabftw bin/init db:populate /seed/populate.yml --yes --fast
    export METARELAY_ELABFTW_TOKEN=metarelay-dev-key
    export METARELAY_ELABFTW_API_URL=http://localhost:3148/api/v2
    python -m examples.save_metadata elabftw://localhost:3148/experiments
    python -m examples.open_elabftw_experiment --uri elabftw://localhost:3148/experiments \\
        --path "Fiber study/Cohort A"

The first command writes a record for the second to load. ``README.md`` in
this folder covers a real eLabFTW.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

import examples.lab_schemas  # noqa: F401  registers lab.implant, so its records load
from examples.login import open_session
from metarelay import (
    ROOT,
    Container,
    LogicalPath,
    MetaRelayError,
    NotFoundError,
    Session,
    StoredRecord,
    render_preview,
)


@dataclass(frozen=True)
class OpenedEntry:
    """What ``open_entry`` found."""

    where: LogicalPath
    containers: list[Container]
    """Containers directly inside ``where``: categories at the root, else entries."""
    record_ids: list[str]
    loaded: StoredRecord | None
    """The record loaded, or ``None`` if ``where`` holds no records."""


def open_entry(
    session: Session, where: LogicalPath = ROOT, record_id: str | None = None
) -> OpenedEntry:
    """List the entries inside ``where``, and load one of its records.

    Loads ``record_id``, or the first record if it is ``None``. The root
    holds categories, and a category holds entries; neither holds records.

    Raises:
        NotFoundError: If there is no such entry or record.
    """
    # [docs:open-entry]
    # At the root, the containers are categories. Below that, eLabFTW has no
    # folders: every container is an entry (an experiment or a resource).
    # holds_records is True for a records-only entry.
    containers = session.list_containers(where)

    # An entry's records are in the entry itself: in its metadata, and shown
    # as tables in its body and as eLabFTW fields.
    records = {}
    if where != ROOT:
        records = {stored.record.record_id: stored for stored in session.list_records(where)}
    # [/docs:open-entry]

    if record_id is not None and record_id not in records:
        raise NotFoundError(f"No record {record_id!r} in {str(where) or 'the root'}.")
    if record_id is None and records:
        record_id = next(iter(records))
    return OpenedEntry(
        where=where,
        containers=containers,
        record_ids=list(records),
        loaded=records[record_id] if record_id is not None else None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m examples.open_elabftw_experiment",
        description="Connect to eLabFTW, list what is there, open an entry, and load a record.",
    )
    parser.add_argument(
        "--uri",
        required=True,
        help="the eLabFTW destination, such as elabftw://elab.example.org/experiments",
    )
    parser.add_argument(
        "--path", default="", help="the entry to open, 'a/b'. Without it, list the top level"
    )
    parser.add_argument("--record", help="the record ID to load (default: the first one)")
    args = parser.parse_args(argv)

    try:
        where = LogicalPath.parse(args.path)
        # The API key comes from METARELAY_ELABFTW_TOKEN; connect() checks it.
        session = open_session(args.uri)
    except MetaRelayError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    try:
        opened = open_entry(session, where, args.record)
    except MetaRelayError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        session.close()

    show(opened)
    return 0


def show(opened: OpenedEntry) -> None:
    print(f"Opened {str(opened.where) or 'the top level'}")
    for container in opened.containers:
        print(f"  {container.name}{' (records only)' if container.holds_records else '/'}")
    if opened.loaded is None:
        if opened.where != ROOT:
            print("No records here. Write one with: python -m examples.save_metadata")
        return
    print(f"Records: {', '.join(opened.record_ids)}")
    stored = opened.loaded
    record = stored.record
    entry = stored.receipt.backend_ids["entry_id"]
    print(f"\n{record.record_id} ({record.schema_id} v{record.schema_version}), entry {entry}:\n")
    # The same tables the entry's body shows in eLabFTW.
    print(render_preview(record).markdown)


if __name__ == "__main__":
    sys.exit(main())
