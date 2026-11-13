"""Log in to LabArchives, list your notebooks, open one, and load a record.

Needs ``pip install 'metarelay[labarchives]'``. Against the fake LabArchives
server, with no account (from the repository root)::

    docker compose up -d
    export METARELAY_LABARCHIVES_AKID=fake-akid METARELAY_LABARCHIVES_AKPASS=fake-akpass
    export METARELAY_LABARCHIVES_API_URL=http://localhost:8080
    python -m examples.save_metadata labarchives://localhost:8080 \\
        --path "Test notebook/Fiber study/Cohort A" --auto-login
    python -m examples.open_labarchives_notebook --uri labarchives://localhost:8080 \\
        --notebook "Test notebook" --path "Fiber study/Cohort A" --auto-login

The first command writes a record for the second to load. ``README.md`` in
this folder covers real LabArchives.

This is the login-and-load step the Chronoxiv migration rebuilds first (Goal 1c).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

import examples.lab_schemas  # noqa: F401  registers lab.implant, so its records load
from examples.login import Browser, ask_in_terminal, follow_fake_login, open_session
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
class OpenedNotebook:
    """What ``open_notebook`` found."""

    notebooks: list[str]
    """The names of every notebook the account can open."""
    where: LogicalPath
    containers: list[Container]
    """Folders and pages directly inside ``where``."""
    record_ids: list[str]
    loaded: StoredRecord | None
    """The record loaded, or ``None`` if ``where`` holds no records."""


def open_notebook(
    session: Session,
    notebook: str,
    inside: LogicalPath = ROOT,
    record_id: str | None = None,
) -> OpenedNotebook:
    """Open ``notebook`` (or a folder or page ``inside`` it) and load one record.

    Loads ``record_id``, or the first record if it is ``None``.

    Raises:
        NotFoundError: If there is no such notebook, folder, page, or record.
    """
    # [docs:open-notebook]
    # A LabArchives session's top-level containers are the account's notebooks.
    notebooks = [container.name for container in session.list_containers(ROOT)]
    if notebook not in notebooks:
        raise NotFoundError(f"No notebook named {notebook!r}. Yours: {', '.join(notebooks)}.")

    # Folders and pages inside it. holds_records is True for a page.
    where = LogicalPath((notebook, *inside.parts))
    containers = session.list_containers(where)

    # Records are found by caption, on the page itself or on the
    # "MetaRelay records" page of a folder.
    records = {stored.record.record_id: stored for stored in session.list_records(where)}
    # [/docs:open-notebook]

    if record_id is not None and record_id not in records:
        raise NotFoundError(f"No record {record_id!r} in {where}.")
    if record_id is None and records:
        record_id = next(iter(records))
    return OpenedNotebook(
        notebooks=notebooks,
        where=where,
        containers=containers,
        record_ids=list(records),
        loaded=records[record_id] if record_id is not None else None,
    )


def main(argv: Sequence[str] | None = None, *, browser: Browser | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m examples.open_labarchives_notebook",
        description="Log in to LabArchives, list your notebooks, open one, and load a record.",
    )
    parser.add_argument(
        "--uri",
        default="labarchives://api.labarchives.com",
        help="the LabArchives destination (default: %(default)s)",
    )
    parser.add_argument("--notebook", help="the notebook to open. Without it, only list them")
    parser.add_argument("--path", default="", help="a folder or page in the notebook, 'a/b'")
    parser.add_argument("--record", help="the record ID to load (default: the first one)")
    parser.add_argument(
        "--auto-login",
        action="store_true",
        help="log in to the fake LabArchives server without a browser",
    )
    args = parser.parse_args(argv)
    if browser is None:
        browser = follow_fake_login if args.auto_login else ask_in_terminal

    try:
        inside = LogicalPath.parse(args.path)
        session = open_session(args.uri, browser=browser)
    except MetaRelayError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    try:
        if args.notebook is None:
            for container in session.list_containers(ROOT):
                print(container.name)
            return 0
        opened = open_notebook(session, args.notebook, inside, args.record)
    except MetaRelayError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        session.close()

    show(opened)
    return 0


def show(opened: OpenedNotebook) -> None:
    print(f"Notebooks: {', '.join(opened.notebooks)}")
    print(f"Opened {opened.where}")
    for container in opened.containers:
        print(f"  {container.name}{' (page)' if container.holds_records else '/'}")
    if opened.loaded is None:
        print("No records here. Write one with: python -m examples.save_metadata")
        return
    print(f"Records: {', '.join(opened.record_ids)}")
    record = opened.loaded.record
    print(f"\n{record.record_id} ({record.schema_id} v{record.schema_version}):\n")
    # The same table the notebook entry shows.
    print(render_preview(record).markdown)


if __name__ == "__main__":
    sys.exit(main())
