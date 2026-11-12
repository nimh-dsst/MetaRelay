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

The conflict token is the SHA-256 of the record's ``.json`` file, so the
receipt changes with any edit to the file. Conflicts are decided on the
record's content, so reformatting the JSON by hand is not a conflict.

Names are compared ignoring case and Unicode spelling on every OS. A
container, record ID or file name that matches an existing one only that way
(``study`` next to ``Study``) is refused, so the backend behaves the same on
macOS, Windows and Linux.

Names that are unsafe on any common filesystem (``\\ : * ? " < > |``, a
trailing dot or space, Windows device names like ``CON``) are rejected on
every platform, so a folder written on a Mac still copies to Windows. Names
starting with ``.`` are reserved for MetaRelay's own files.

Writes are atomic (a temporary file, then a rename), and ``create`` mode is
enforced: two writers cannot both create the same record. (On a drive without
hard links, such as exFAT, a ``create`` is enforced but not atomic.) Other modes assume
one writer per container at a time
(``list_records`` still finds every record if two writers race on the index).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import unicodedata
import uuid
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
PREVIEWS_DIR = "previews"
"""``.metarelay/previews/<record_id>`` holds the ``preview_checksum`` of that
record's preview as MetaRelay last wrote it. One file per record, so two
writers in the same folder can't lose each other's."""
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
    except (NotADirectoryError, IsADirectoryError) as error:
        raise ValidationError(
            f"Could not {action}: a file and a folder have the same name ({error.filename})."
        ) from error
    except OSError as error:
        raise BackendUnavailableError(f"Could not {action}: {error.strerror or error}.") from error


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(target: Path, data: bytes, *, exclusive: bool = False) -> None:
    """Write ``data`` to ``target`` so readers never see a half-written file.

    With ``exclusive``, raise ``FileExistsError`` if ``target`` exists, even if
    another process creates it at the same moment.

    The file gets the usual permissions (``0o666`` minus the umask), so a
    shared lab folder stays readable by the group. ``tempfile.mkstemp`` would
    make every file readable only by its writer.
    """
    temp = target.with_name(f".tmp-{uuid.uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    handle = os.open(temp, flags, 0o666)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if not exclusive:
            os.replace(temp, target)
            return
        try:
            os.link(temp, target)  # atomic, and fails if target exists
        except FileExistsError:
            raise
        except OSError:
            # No hard links here (exFAT, some network drives). Exclusive
            # create still never overwrites, but isn't atomic: a crash
            # mid-write can leave a partial file, which reads as damaged.
            with open(target, "xb") as file:
                file.write(data)
    finally:
        temp.unlink(missing_ok=True)


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _fold(name: str) -> str:
    """How names are compared: ignoring case and Unicode spelling, on every OS.

    macOS and Windows disks treat ``Study``/``study`` (and the two Unicode
    spellings of ``ä``) as one name; Linux doesn't. Comparing folded names
    everywhere makes the backend behave the same on all three.
    """
    return _nfc(name).casefold()


def _nfc_path(path: LogicalPath) -> LogicalPath:
    return LogicalPath(tuple(_nfc(part) for part in path.parts))


def _entries(folder: Path) -> dict[str, str]:
    """Everything in ``folder``: NFC name -> name as stored on disk."""
    return {_nfc(name): name for name in os.listdir(folder)}


def _twin(entries: dict[str, str], name: str) -> str | None:
    """A stored name equal to ``name`` once folded, but spelled differently."""
    for nfc_name, stored in entries.items():
        if nfc_name != name and _fold(nfc_name) == _fold(name):
            return stored
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
        parent = _nfc_path(parent)
        folder = self._walk(parent, create=False)
        with _disk_errors(f"list {self._describe(parent)}"):
            names = sorted(
                nfc_name
                for nfc_name, stored in _entries(folder).items()
                if (folder / stored).is_dir()
                and not nfc_name.startswith(".")
                and _name_problem(nfc_name) is None
            )
        return [Container(path=parent / name) for name in names]

    def ensure_container(self, path: LogicalPath, *, holds_records: bool = False) -> Container:
        # Every container is a folder here, so holds_records changes nothing.
        path = _nfc_path(path)
        self._walk(path, create=True)
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
        path = _nfc_path(path)
        folder = self._walk(path, create=False)
        record_id = check_local_name(record.record_id, "Record ID")
        json_name, md_name = f"{record_id}.json", f"{record_id}.md"
        if _name_problem(json_name):
            raise ValidationError(f"Record ID {record_id!r} is too long to use as a file name.")
        json_file, md_file = folder / json_name, folder / md_name
        where = f"write {record_id!r} in {self._describe(path)}"

        with _disk_errors(where):
            self._refuse_name_clash(folder, record_id)
            if json_file.is_dir():
                raise ValidationError(f"{json_name!r} is a folder at the destination.")
            remote_bytes = _read_optional(json_file)
            remote_preview = _read_optional(md_file)
            remote_checksum = None
            if remote_bytes is not None:
                remote_checksum = self._checksum_on_disk(remote_bytes)
            action = decide_write(  # also rejects an unknown mode
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
            mode = WriteMode(mode)

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
                self._remember_preview(folder, record_id, preview)
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
                    self._remember_preview(folder, record_id, preview)
                self._update_index(folder, records={record_id})

        return self._read(path, folder, record_id).receipt

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        path = _nfc_path(path)
        folder = self._walk(path, create=False)
        if _name_problem(record_id) or _name_problem(f"{record_id}.json"):
            raise NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        return self._read(path, folder, record_id)

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        path = _nfc_path(path)
        folder = self._walk(path, create=False)
        with _disk_errors(f"list records in {self._describe(path)}"):
            index = self._load_index(folder)
            # Also scan, so a record whose index entry was lost (two writers
            # updating the index at once, say) still shows up.
            files = {_fold(name) for name in index["files"]}
            ids = set(index["records"]) | set(self._scan_records(folder, files))
            ids = {record_id for record_id in ids if _fold(f"{record_id}.json") not in files}
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
        path = _nfc_path(path)
        folder = self._walk(path, create=False)
        name = _nfc(check_local_name(name, "File name"))
        data = bytes(data)
        with _disk_errors(f"write {name!r} in {self._describe(path)}"):
            index = self._load_index(folder)
            entries = _entries(folder)
            target = folder / entries.get(name, name)
            if target.is_dir():
                raise ValidationError(f"{name!r} is a folder at the destination.")
            twin = _twin(entries, name)
            if twin is not None:
                raise ValidationError(
                    f"{name!r} differs only in case from {twin!r}, which is already here. "
                    "Use the same spelling, or another name."
                )
            # Never take a name that belongs to a record or its preview, even
            # if the file is missing or the index forgot the record.
            files = {_fold(each) for each in index["files"]}
            record_ids = set(index["records"]) | set(self._scan_records(folder, files))
            owned = {_fold(f"{rid}{suffix}") for rid in record_ids for suffix in (".json", ".md")}
            if _fold(name) in owned:
                raise ValidationError(
                    f"{name!r} is a record's file name. Use another name for this file."
                )
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

    def _walk(self, path: LogicalPath, *, create: bool) -> Path:
        """The folder for ``path``, as spelled on disk. Creates it if ``create``.

        A name that matches an existing one only when case or Unicode spelling
        is ignored is refused, so ``study`` can't silently mean ``Study`` on a
        Mac while being a different folder on Linux.
        """
        for part in path.parts:  # check every name before creating anything
            check_local_name(part, "Container name")
        folder = self.root
        so_far: list[str] = []
        for part in path.parts:
            so_far.append(part)
            here = LogicalPath(tuple(so_far))
            with _disk_errors(f"open {self._describe(here)}"):
                entries = _entries(folder)
                stored = entries.get(part)
                if stored is None:
                    twin = _twin(entries, part)
                    if twin is not None:
                        raise ValidationError(
                            f"{self._describe(here)} differs only in case from {twin!r}, "
                            "which already exists. Use the existing spelling."
                        )
                    if not create:
                        raise NotFoundError(
                            f"{self._describe(here)} does not exist. Create it first "
                            "(ensure_container(), or metarelay mkdir)."
                        )
                    (folder / part).mkdir(exist_ok=True)
                    stored = part
                folder = folder / stored
                if not folder.is_dir():
                    raise ValidationError(f"{self._describe(here)} is a file, not a folder.")
        return folder

    def _describe(self, path: LogicalPath) -> str:
        return f"container {str(path)!r}" if path.parts else "the destination root"

    def _refuse_name_clash(self, folder: Path, record_id: str) -> None:
        """Refuse a record whose files would collide with another name here."""
        names = (f"{record_id}.json", f"{record_id}.md")
        files = {_fold(name) for name in self._load_index(folder)["files"]}
        for name in names:
            if _fold(name) in files:
                raise ValidationError(
                    f"{name!r} is a file written with write_file, not a record. "
                    "Use another record ID."
                )
        entries = _entries(folder)
        for name in names:
            twin = _twin(entries, name)
            if twin is not None:
                raise ValidationError(
                    f"Record ID {record_id!r} differs only in case from {twin!r}, which is "
                    "already here. IDs that differ only in case can't share a container."
                )

    def _checksum_on_disk(self, data: bytes) -> str:
        """``record_checksum`` of what is on disk, or a value that matches nothing."""
        try:
            on_disk = Record.model_validate_json(data)
        except ValidationError:
            return "unreadable:" + _sha256(data)
        return record_checksum(on_disk)

    def _read(self, path: LogicalPath, folder: Path, record_id: str) -> StoredRecord:
        json_file = folder / f"{record_id}.json"
        missing = NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        with _disk_errors(f"read {record_id!r} in {self._describe(path)}"):
            files = {_fold(name) for name in self._load_index(folder)["files"]}
            if _fold(f"{record_id}.json") in files:
                raise missing
            # Look the name up exactly: a case-insensitive disk would open
            # implant-M1.json for "Implant-M1".
            if f"{record_id}.json" not in _entries(folder):
                raise missing
            data = _read_optional(json_file)
            preview = _read_optional(folder / f"{record_id}.md")
            written_preview = self._written_preview(folder, record_id)
        if data is None:
            raise missing
        try:
            record = Record.model_validate_json(data)
        except ValidationError as error:
            raise ValidationError(f"{json_file} is not a valid record: {error}") from error
        if record.record_id != record_id:
            raise ValidationError(
                f"{json_file} holds record {record.record_id!r}, not {record_id!r}. "
                "Rename the file or fix its record_id."
            )
        receipt = Receipt(
            destination=self.destination,
            backend=BACKEND_NAME,
            container=Container(path=path),
            record_id=record.record_id,
            schema_id=record.schema_id,
            schema_version=record.schema_version,
            checksum=record_checksum(record),
            conflict_token=_sha256(data),
            # The preview as MetaRelay last wrote it, so a hand edit still
            # conflicts after a read. Records written before MetaRelay kept
            # these fall back to the preview as it is now.
            preview_checksum=written_preview
            or (
                preview_checksum(preview.decode(errors="replace")) if preview is not None else None
            ),
        )
        return StoredRecord(record=record, receipt=receipt)

    def _written_preview(self, folder: Path, record_id: str) -> str | None:
        where = folder / META_DIR / PREVIEWS_DIR / record_id
        data = _read_optional(where)
        if data is None:
            return None
        checksum = data.decode(errors="replace").strip()
        if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
            raise ValidationError(
                f"{where} is damaged. Delete it; MetaRelay then compares edits "
                "against the preview as it is now."
            )
        return checksum

    def _remember_preview(self, folder: Path, record_id: str, preview: str) -> None:
        previews = folder / META_DIR / PREVIEWS_DIR
        previews.mkdir(parents=True, exist_ok=True)
        _write_atomic(previews / record_id, (preview_checksum(preview) + "\n").encode())

    def _load_index(self, folder: Path) -> dict[str, list[str]]:
        data = _read_optional(folder / META_DIR / INDEX_FILE)
        if data is None:
            return {"records": self._scan_records(folder, set()), "files": []}
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

    def _scan_records(self, folder: Path, files: AbstractSet[str]) -> list[str]:
        """Every ``{id}.json`` here that isn't a ``write_file`` output.

        Not parsed: a damaged record still counts, so reading it fails loudly
        instead of the record silently disappearing from a listing.
        """
        found = []
        for name in sorted(_entries(folder)):
            record_id = name.removesuffix(".json")
            if (
                name.endswith(".json")
                and not name.startswith(".")
                and _name_problem(record_id) is None
                and _fold(name) not in files
                and (folder / name).is_file()
            ):
                found.append(record_id)
        return found

    def _update_index(
        self,
        folder: Path,
        *,
        records: AbstractSet[str] = frozenset(),
        files: AbstractSet[str] = frozenset(),
    ) -> None:
        index = self._load_index(folder)
        new_files = set(index["files"]) | files
        # A write_file output is never a record, even one a scan picked up.
        file_names = {_fold(name) for name in new_files}
        new_records = {
            record_id
            for record_id in set(index["records"]) | records
            if _fold(f"{record_id}.json") not in file_names
        }
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
        # One spelling per folder, so receipts are found whichever way the URI
        # was typed: "lab data" or "lab%20data", /tmp or /private/tmp.
        root = root.resolve()
        return LocalSession(root, Destination.parse(root.as_uri()).uri)

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
