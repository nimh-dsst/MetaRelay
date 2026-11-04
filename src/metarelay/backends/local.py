"""The local backend: records as files in a directory tree (``file://``).

The reference backend. It works offline, needs no credentials, and runs the
conformance suite in CI. Layout under the destination folder::

    file:///Users/me/metadata
    └── Fiber study/                 a container (LogicalPath ("Fiber study",))
        ├── implant-M1.json          the record: the source of truth
        ├── implant-M1.md            readable preview, regenerated on every write
        ├── trace.csv                a file from write_file
        └── .metarelay/
            ├── index.json           which files here are records; rebuilt if deleted
            └── history.jsonl        one line per write, appended

The conflict token is the SHA-256 of the record's ``.json`` file, so any
change to the file, even one that leaves the record content alone, shows up.

Names that are unsafe on any common filesystem (``\\ : * ? " < > |``, a
trailing dot or space, Windows device names like ``CON``) are rejected on
every platform, so a folder written on a Mac still copies to Windows. Names
starting with ``.`` are reserved for MetaRelay's own files.

Writes are atomic (a temporary file, then a rename), and ``create`` mode is
enforced: two writers cannot both create the same record. Other modes assume
one writer per container at a time
(``list_records`` still finds every record if two writers race on the index).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import unicodedata
from collections.abc import Iterator, Mapping
from collections.abc import Set as AbstractSet
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from urllib.request import url2pathname

from metarelay.core.auth import AuthFlow, LoginChallenge
from metarelay.core.checksum import preview_checksum, record_checksum
from metarelay.core.config import DestinationConfig
from metarelay.core.conflict import WriteAction, WriteMode, decide_write
from metarelay.core.destination import Destination
from metarelay.core.errors import (
    BackendUnavailableError,
    ConfigurationError,
    ConflictError,
    MetaRelayError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from metarelay.core.path import ROOT, Container, LogicalPath
from metarelay.core.preview import render_preview
from metarelay.core.receipt import FileReceipt, Receipt
from metarelay.core.record import Record
from metarelay.core.session import StoredRecord

__all__ = ["LocalBackend", "LocalSession", "check_local_name"]

BACKEND_NAME = "local"
META_DIR = ".metarelay"
INDEX_FILE = "index.json"
HISTORY_FILE = "history.jsonl"
INDEX_VERSION = 1

_UNSAFE_CHARS = frozenset('\\:*?"<>|')
_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)
_MAX_NAME_BYTES = 255


def check_local_name(name: str, what: str = "Name") -> str:
    """Raise ``ValidationError`` if ``name`` can't be a file or folder name everywhere."""
    problem = _name_problem(name)
    if problem:
        raise ValidationError(f"{what} {name!r} can't be used on disk: {problem}.")
    return name


def _name_problem(name: str) -> str | None:
    if not name or name in {".", ".."}:
        return "it is empty or '.' or '..'"
    if "/" in name or any(char in _UNSAFE_CHARS for char in name):
        return 'it contains one of / \\ : * ? " < > |'
    if any(unicodedata.category(char) == "Cc" for char in name):
        return "it contains a control character"
    if name.startswith("."):
        return "names starting with '.' are reserved for MetaRelay"
    if name.endswith((".", " ")):
        return "it ends with a period or space"
    if name.split(".")[0].upper().rstrip() in _DEVICE_NAMES:
        return "it is a reserved device name on Windows"
    if len(name.encode()) > _MAX_NAME_BYTES:
        return f"it is longer than {_MAX_NAME_BYTES} bytes"
    return None


@contextlib.contextmanager
def _disk_errors(action: str) -> Iterator[None]:
    """Translate ``OSError`` into MetaRelay errors."""
    try:
        yield
    except MetaRelayError:
        raise
    except FileNotFoundError as error:
        raise NotFoundError(f"Could not {action}: {error.strerror} ({error.filename}).") from error
    except PermissionError as error:
        raise PermissionDeniedError(
            f"Could not {action}: permission denied ({error.filename})."
        ) from error
    except NotADirectoryError as error:
        raise ConflictError(
            f"Could not {action}: {error.filename} is a file, not a folder."
        ) from error
    except OSError as error:
        raise BackendUnavailableError(f"Could not {action}: {error.strerror or error}.") from error


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(target: Path, data: bytes, *, exclusive: bool = False) -> None:
    """Write ``data`` to ``target`` so readers never see a half-written file.

    With ``exclusive``, raise ``FileExistsError`` if ``target`` exists, even if
    another process creates it at the same moment.
    """
    handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if exclusive:
            os.link(temp, target)  # fails if target exists; atomic
        else:
            os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


class LocalSession:
    """A session on a folder. Create it with ``connect("file:///path")``."""

    def __init__(self, root: Path, destination: str) -> None:
        self.root = root
        self._destination = destination

    @property
    def destination(self) -> str:
        return self._destination

    # Containers

    def list_containers(self, parent: LogicalPath = ROOT) -> list[Container]:
        folder = self._existing_folder(parent)
        with _disk_errors(f"list {self._describe(parent)}"):
            names = sorted(
                entry.name
                for entry in folder.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            )
        return [Container(path=parent / name) for name in names if _name_problem(name) is None]

    def ensure_container(self, path: LogicalPath) -> Container:
        folder = self._folder(path)
        with _disk_errors(f"create {self._describe(path)}"):
            folder.mkdir(parents=True, exist_ok=True)
        return Container(path=path)

    # Records

    def write_record(
        self,
        path: LogicalPath,
        record: Record,
        *,
        mode: WriteMode = WriteMode.UPSERT,
        prior_receipt: Receipt | None = None,
    ) -> Receipt:
        mode = WriteMode(mode)
        folder = self._existing_folder(path)
        record_id = check_local_name(record.record_id, "Record ID")
        json_file, md_file = folder / f"{record_id}.json", folder / f"{record_id}.md"
        where = f"write {record_id!r} in {self._describe(path)}"

        with _disk_errors(where):
            self._refuse_file_name_clash(folder, record_id)
            remote_bytes = _read_optional(json_file)
            remote_preview = _read_optional(md_file)
            remote_checksum = None
            if remote_bytes is not None:
                remote_checksum = self._checksum_on_disk(remote_bytes, record_id)
            action = decide_write(
                mode=mode,
                local_checksum=record_checksum(record),
                remote_checksum=remote_checksum,
                prior_receipt=prior_receipt,
                remote_preview_checksum=(
                    preview_checksum(remote_preview.decode(errors="replace"))
                    if remote_preview is not None
                    else None
                ),
            )

            preview = render_preview(record).markdown
            if action is WriteAction.WRITE:
                data = (record.model_dump_json(indent=2) + "\n").encode()
                try:
                    _write_atomic(json_file, data, exclusive=mode is WriteMode.CREATE)
                except FileExistsError as error:
                    raise ConflictError(
                        "A record with this ID already exists at the destination."
                    ) from error
                _write_atomic(md_file, preview.encode())
                self._update_index(folder, records={record_id})
                self._append_history(
                    folder,
                    kind="record",
                    name=record_id,
                    action="create" if remote_bytes is None else "update",
                    mode=mode.value,
                    checksum=record_checksum(record),
                    previous_checksum=remote_checksum,
                )
            else:
                # Same content: leave it alone, but restore a missing preview
                # and index entry. An edited preview is not touched.
                if remote_preview is None:
                    _write_atomic(md_file, preview.encode())
                self._update_index(folder, records={record_id})

        return self._read(path, folder, record_id).receipt

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        folder = self._existing_folder(path)
        if _name_problem(record_id):
            raise NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        return self._read(path, folder, record_id)

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        folder = self._existing_folder(path)
        with _disk_errors(f"list records in {self._describe(path)}"):
            index = self._load_index(folder)
            # Also scan, so a record whose index entry was lost (two writers
            # updating the index at once, say) still shows up.
            ids = set(index["records"]) | set(self._scan_records(folder))
            ids -= {name.removesuffix(".json") for name in index["files"]}
        found = []
        for record_id in sorted(ids):
            try:
                found.append(self._read(path, folder, record_id))
            except NotFoundError:
                continue  # deleted by hand since it was indexed
        return found

    # Files

    def write_file(
        self,
        path: LogicalPath,
        name: str,
        data: bytes,
        *,
        media_type: str | None = None,
    ) -> FileReceipt:
        folder = self._existing_folder(path)
        check_local_name(name, "File name")
        data = bytes(data)
        with _disk_errors(f"write {name!r} in {self._describe(path)}"):
            index = self._load_index(folder)
            stem, _, suffix = name.rpartition(".")
            if suffix in {"json", "md"} and stem in index["records"]:
                raise ConflictError(f"{name!r} would overwrite the record {stem!r}.")
            target = folder / name
            if target.is_dir():
                raise ConflictError(f"{name!r} is a folder at the destination.")
            _write_atomic(target, data)
            self._update_index(folder, files={name})
            checksum = _sha256(data)
            self._append_history(folder, kind="file", name=name, action="write", checksum=checksum)
        return FileReceipt(
            destination=self.destination,
            backend=BACKEND_NAME,
            container=Container(path=path),
            name=name,
            checksum=checksum,
            size=len(data),
        )

    def close(self) -> None:
        """Nothing to release; here for the protocol."""

    # Helpers

    def _folder(self, path: LogicalPath) -> Path:
        for name in path.parts:
            check_local_name(name, "Container name")
        return self.root.joinpath(*path.parts)

    def _existing_folder(self, path: LogicalPath) -> Path:
        folder = self._folder(path)
        if not folder.is_dir():
            raise NotFoundError(
                f"{self._describe(path)} does not exist. Create it first "
                "(ensure_container(), or metarelay mkdir)."
            )
        return folder

    def _describe(self, path: LogicalPath) -> str:
        return f"container {str(path)!r}" if path.parts else "the destination root"

    def _refuse_file_name_clash(self, folder: Path, record_id: str) -> None:
        files = self._load_index(folder)["files"]
        for name in (f"{record_id}.json", f"{record_id}.md"):
            if name in files:
                raise ConflictError(f"{name!r} is a file written with write_file, not a record.")

    def _checksum_on_disk(self, data: bytes, record_id: str) -> str:
        """``record_checksum`` of what is on disk, or a value that matches nothing."""
        try:
            on_disk = Record.model_validate_json(data)
        except ValidationError:
            return "unreadable:" + _sha256(data)
        if on_disk.record_id != record_id:
            # A case-insensitive disk (macOS, Windows) found "Rec-1.json" for "rec-1".
            return "other-record:" + _sha256(data)
        return record_checksum(on_disk)

    def _read(self, path: LogicalPath, folder: Path, record_id: str) -> StoredRecord:
        json_file = folder / f"{record_id}.json"
        missing = NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        with _disk_errors(f"read {record_id!r} in {self._describe(path)}"):
            if f"{record_id}.json" in self._load_index(folder)["files"]:
                raise missing
            data = _read_optional(json_file)
            preview = _read_optional(folder / f"{record_id}.md")
        if data is None:
            raise missing
        try:
            record = Record.model_validate_json(data)
        except ValidationError as error:
            raise ValidationError(f"{json_file} is not a valid record: {error}") from error
        if record.record_id != record_id:
            raise missing
        receipt = Receipt(
            destination=self.destination,
            backend=BACKEND_NAME,
            container=Container(path=path),
            record_id=record.record_id,
            schema_id=record.schema_id,
            schema_version=record.schema_version,
            checksum=record_checksum(record),
            conflict_token=_sha256(data),
            preview_checksum=(
                preview_checksum(preview.decode(errors="replace")) if preview is not None else None
            ),
        )
        return StoredRecord(record=record, receipt=receipt)

    def _load_index(self, folder: Path) -> dict[str, list[str]]:
        data = _read_optional(folder / META_DIR / INDEX_FILE)
        if data is None:
            return {"records": self._scan_records(folder), "files": []}
        try:
            index = json.loads(data)
            records, files = index["records"], index["files"]
            if not all(
                isinstance(names, list) and all(isinstance(name, str) for name in names)
                for names in (records, files)
            ):
                raise TypeError("records and files must be lists of names")
            return {"records": records, "files": files}
        except (ValueError, KeyError, TypeError) as error:
            raise ValidationError(
                f"{folder / META_DIR / INDEX_FILE} is damaged. Delete it, and MetaRelay "
                "will rebuild it from the .json files."
            ) from error

    def _scan_records(self, folder: Path) -> list[str]:
        """Find records without an index: every ``{id}.json`` that holds record ``id``."""
        found = []
        for candidate in sorted(folder.glob("*.json")):
            if candidate.name.startswith(".") or not candidate.is_file():
                continue
            try:
                record = Record.model_validate_json(candidate.read_bytes())
            except ValidationError:
                continue
            if record.record_id == candidate.stem:
                found.append(record.record_id)
        return found

    def _update_index(
        self,
        folder: Path,
        *,
        records: AbstractSet[str] = frozenset(),
        files: AbstractSet[str] = frozenset(),
    ) -> None:
        index = self._load_index(folder)
        new_records = set(index["records"]) | records
        new_files = (set(index["files"]) | files) - new_records
        meta = folder / META_DIR
        unchanged = new_records == set(index["records"]) and new_files == set(index["files"])
        if unchanged and (meta / INDEX_FILE).exists():
            return
        meta.mkdir(exist_ok=True)
        content = {
            "index_version": INDEX_VERSION,
            "records": sorted(new_records),
            "files": sorted(new_files),
        }
        _write_atomic(meta / INDEX_FILE, (json.dumps(content, indent=2) + "\n").encode())

    def _append_history(
        self,
        folder: Path,
        *,
        kind: Literal["record", "file"],
        name: str,
        action: str,
        **details: Any,
    ) -> None:
        meta = folder / META_DIR
        meta.mkdir(exist_ok=True)
        entry = {
            "at": datetime.now().astimezone().isoformat(),
            "kind": kind,
            "name": name,
            "action": action,
            **details,
        }
        with (meta / HISTORY_FILE).open("a", encoding="utf-8") as history:
            history.write(json.dumps(entry, ensure_ascii=False) + "\n")


class LocalBackend:
    """The ``file://`` backend. Registered under the ``metarelay.backends`` entry points."""

    name = BACKEND_NAME
    auth_flow = AuthFlow.NONE

    def open(self, destination: Destination, config: DestinationConfig | None) -> LocalSession:
        if destination.netloc not in {"", "localhost"}:
            raise ConfigurationError(
                f"{destination.uri} names a host. The local backend only reads this "
                "computer's disk: use file:///path/to/folder (three slashes)."
            )
        parts = urlsplit(destination.uri)
        if parts.query or parts.fragment:
            raise ConfigurationError(f"{destination.uri} cannot have a query or fragment.")
        root = Path(url2pathname(destination.path))
        if not root.is_dir():
            raise ConfigurationError(
                f"The folder {root} does not exist. Create it first, then connect again."
            )
        return LocalSession(root, destination.uri)

    def begin_login(
        self, destination: Destination, redirect_url: str, config: DestinationConfig | None
    ) -> LoginChallenge:
        raise ConfigurationError("file:// does not use a browser login.")

    def complete_login(
        self,
        challenge: LoginChallenge,
        params: Mapping[str, str],
        config: DestinationConfig | None,
    ) -> LocalSession:
        raise ConfigurationError("file:// does not use a browser login.")
