"""Connect to GitHub with a token, list what is there, open a folder, and load a record.

Needs ``pip install 'metarelay[github]'``. Against the fake GitHub API, with
no account (from the repository root, in two terminals)::

    python -m metarelay.testing.github_fake --port 8081 --repo my-lab/metadata --token fake-token

    export METARELAY_GITHUB_TOKEN=fake-token METARELAY_GITHUB_API_URL=http://127.0.0.1:8081
    python -m examples.save_metadata github://my-lab/metadata@main/records
    python -m examples.open_github_repo --uri github://my-lab/metadata@main/records \\
        --path "Fiber study/Cohort A"

The first command writes a record for the second to load. ``README.md`` in
this folder covers real GitHub.
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
class OpenedFolder:
    """What ``open_folder`` found."""

    where: LogicalPath
    containers: list[Container]
    """Folders directly inside ``where``."""
    record_ids: list[str]
    loaded: StoredRecord | None
    """The record loaded, or ``None`` if ``where`` holds no records."""


def open_folder(
    session: Session, where: LogicalPath = ROOT, record_id: str | None = None
) -> OpenedFolder:
    """List the folders inside ``where``, and load one of its records.

    Loads ``record_id``, or the first record if it is ``None``.

    Raises:
        NotFoundError: If there is no such folder or record.
    """
    # [docs:open-folder]
    # Containers are folders in the repository, under the URI's prefix.
    containers = session.list_containers(where)

    # Each record is a .json file, with a .md preview beside it that GitHub
    # shows as tables.
    records = {stored.record.record_id: stored for stored in session.list_records(where)}
    # [/docs:open-folder]

    if record_id is not None and record_id not in records:
        raise NotFoundError(f"No record {record_id!r} in {str(where) or 'the top level'}.")
    if record_id is None and records:
        record_id = next(iter(records))
    return OpenedFolder(
        where=where,
        containers=containers,
        record_ids=list(records),
        loaded=records[record_id] if record_id is not None else None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m examples.open_github_repo",
        description="Connect to GitHub, list what is there, open a folder, and load a record.",
    )
    parser.add_argument(
        "--uri",
        required=True,
        help="the GitHub destination, such as github://my-lab/metadata@main/records",
    )
    parser.add_argument(
        "--path", default="", help="the folder to open, 'a/b'. Without it, the top level"
    )
    parser.add_argument("--record", help="the record ID to load (default: the first one)")
    args = parser.parse_args(argv)

    try:
        where = LogicalPath.parse(args.path)
        # The token comes from METARELAY_GITHUB_TOKEN; connect() checks it.
        session = open_session(args.uri)
    except MetaRelayError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    try:
        opened = open_folder(session, where, args.record)
    except MetaRelayError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        session.close()

    show(opened)
    return 0


def show(opened: OpenedFolder) -> None:
    print(f"Opened {str(opened.where) or 'the top level'}")
    for container in opened.containers:
        print(f"  {container.name}/")
    if opened.loaded is None:
        print("No records here. Write one with: python -m examples.save_metadata")
        return
    print(f"Records: {', '.join(opened.record_ids)}")
    stored = opened.loaded
    record = stored.record
    path = stored.receipt.backend_ids["path"]
    print(f"\n{record.record_id} ({record.schema_id} v{record.schema_version}), {path}:\n")
    # The same tables GitHub shows for the .md file beside the record.
    print(render_preview(record).markdown)


if __name__ == "__main__":
    sys.exit(main())
