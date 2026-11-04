"""The ``metarelay`` command: try MetaRelay from a terminal.

::

    metarelay ls    file:///data/metadata "Fiber study"
    metarelay mkdir file:///data/metadata "Fiber study/Cohort A"
    metarelay write file:///data/metadata "Fiber study/Cohort A" implant-M1.json
    metarelay read  file:///data/metadata "Fiber study/Cohort A" implant-M1

Paths are ``/``-separated container names; leave the path out for the root.
Records are validated against registered schemas, so pass ``--import`` with
the module that registers them (until MetaRelay ships its own schemas).

``--receipts FILE`` keeps receipts in a JSON file between runs, so updating a
record you wrote earlier is not reported as a conflict.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from metarelay.core.backend import connect
from metarelay.core.conflict import WriteMode
from metarelay.core.errors import (
    ConfigurationError,
    ConflictError,
    MetaRelayError,
    NotFoundError,
    ValidationError,
)
from metarelay.core.path import LogicalPath
from metarelay.core.receipt import SyncIndex
from metarelay.core.record import Record
from metarelay.core.session import Session

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metarelay",
        description="Write and read metadata records at a destination URI.",
    )
    parser.add_argument(
        "--import",
        dest="imports",
        action="append",
        default=[],
        metavar="MODULE",
        help="import a Python module first (one that registers schemas); repeatable",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    ls = commands.add_parser("ls", help="list the containers and records in a container")
    _add_location(ls, path_required=False)

    mkdir = commands.add_parser("mkdir", help="create a container (and its parents)")
    _add_location(mkdir, path_required=True)

    write = commands.add_parser("write", help="write a record from a JSON file")
    _add_location(write, path_required=True)
    write.add_argument("file", help="the record as JSON, or - for standard input")
    write.add_argument(
        "--mode",
        choices=[mode.value for mode in WriteMode],
        default=WriteMode.UPSERT.value,
        help="create: fail if it exists; upsert (default): update unless it changed "
        "remotely; force: overwrite",
    )
    write.add_argument(
        "--create-path",
        action="store_true",
        help="create the container first if it doesn't exist (otherwise a missing "
        "container is an error, so a typo doesn't create a folder)",
    )
    _add_receipts(write)

    read = commands.add_parser("read", help="print a record as JSON")
    _add_location(read, path_required=True)
    read.add_argument("record_id", help="the record's ID")
    _add_receipts(read)
    return parser


def _add_location(parser: argparse.ArgumentParser, *, path_required: bool) -> None:
    parser.add_argument("uri", help="destination URI, for example file:///data/metadata")
    parser.add_argument(
        "path",
        nargs=None if path_required else "?",
        default="",
        help="container path, for example 'Fiber study/Cohort A'"
        + ("" if path_required else " (default: the root)"),
    )


def _add_receipts(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--receipts", type=Path, metavar="FILE", help="read and save receipts in this JSON file"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line. Returns the exit status."""
    args = _parser().parse_args(argv)
    try:
        _check_receipts_file(getattr(args, "receipts", None))
        for module in args.imports:
            _import(module)
        session = connect(args.uri)
        try:
            return _run(args, session)
        finally:
            session.close()
    except ConflictError as error:
        print(f"metarelay: conflict: {error}", file=sys.stderr)
        if args.command == "write" and args.mode != WriteMode.FORCE.value:
            if args.receipts is None and args.mode == WriteMode.UPSERT.value:
                print("If you wrote it earlier, pass the same --receipts file.", file=sys.stderr)
            print("To overwrite it anyway, re-run with --mode force.", file=sys.stderr)
        return 3
    except MetaRelayError as error:
        print(f"metarelay: {error}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, session: Session) -> int:
    path = LogicalPath.parse(args.path)

    if args.command == "ls":
        for container in session.list_containers(path):
            print(f"{container.name}/")
        for stored in session.list_records(path):
            record = stored.record
            print(f"{record.record_id}\t{record.schema_id} v{record.schema_version}")
        return 0

    if args.command == "mkdir":
        session.ensure_container(path)
        return 0

    receipts = _load_receipts(args.receipts)

    if args.command == "write":
        record = Record.model_validate_json(_read_input(args.file))
        if args.create_path:
            session.ensure_container(path)
        before = _state_at_destination(session, path, record.record_id)
        receipt = session.write_record(
            path,
            record,
            mode=WriteMode(args.mode),
            prior_receipt=receipts.get(session.destination, path, record.record_id),
        )
        receipts.put(receipt)
        _save_receipts(args.receipts, receipts)
        unchanged = before == (receipt.checksum, receipt.conflict_token, receipt.preview_checksum)
        done = "Already up to date:" if unchanged else "Wrote"
        print(f"{done} {record.record_id} ({receipt.checksum[:12]})", file=sys.stderr)
        return 0

    # read
    stored = session.read_record(path, args.record_id)
    receipts.put(stored.receipt)
    _save_receipts(args.receipts, receipts)
    print(stored.record.model_dump_json(indent=2))
    return 0


def _state_at_destination(
    session: Session, path: LogicalPath, record_id: str
) -> tuple[str, str, str | None] | None:
    """What's there now (record, file and preview fingerprints), or ``None``."""
    try:
        receipt = session.read_record(path, record_id).receipt
    except (NotFoundError, ValidationError):
        return None
    return receipt.checksum, receipt.conflict_token, receipt.preview_checksum


def _check_receipts_file(file: Path | None) -> None:
    """Fail before writing anything if the receipts can't be saved afterwards."""
    if file is None:
        return
    folder = file.parent
    if not folder.is_dir():
        raise ValidationError(f"Can't save receipts to {file}: the folder {folder} doesn't exist.")
    if not os.access(folder, os.W_OK) or (file.exists() and not os.access(file, os.W_OK)):
        raise ValidationError(f"Can't save receipts to {file}: permission denied.")


def _import_path() -> None:
    """Let --import find modules in the current folder.

    ``python script.py`` puts the script's folder on the path, but an
    installed console script doesn't, so ``--import lab_schemas`` would
    miss ./lab_schemas.py.
    """
    cwd = os.getcwd()
    if cwd not in sys.path and "" not in sys.path:
        sys.path.insert(0, cwd)


def _import(module: str) -> None:
    _import_path()
    try:
        importlib.import_module(module)
    except MetaRelayError:
        raise
    except Exception as error:  # a module can fail to import in any way
        raise ConfigurationError(f"Could not import {module!r}: {error}") from error


def _read_input(name: str) -> bytes:
    if name == "-":
        return sys.stdin.buffer.read()
    try:
        return Path(name).read_bytes()
    except OSError as error:
        raise ValidationError(f"Could not read {name}: {error.strerror}.") from error


def _load_receipts(file: Path | None) -> SyncIndex:
    if file is None or not file.exists():
        return SyncIndex()
    try:
        return SyncIndex.model_validate_json(file.read_bytes())
    except OSError as error:
        raise ValidationError(f"Could not read {file}: {error.strerror}.") from error


def _save_receipts(file: Path | None, receipts: SyncIndex) -> None:
    if file is None:
        return
    # Write a temporary file, then rename it, so a crash can't leave a
    # half-written receipts file that blocks every later run.
    temp = file.with_name(f".{file.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(receipts.model_dump(mode="json"), indent=2) + "\n")
        os.replace(temp, file)
    except OSError as error:
        raise ValidationError(f"Could not save receipts to {file}: {error.strerror}.") from error
    finally:
        temp.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
