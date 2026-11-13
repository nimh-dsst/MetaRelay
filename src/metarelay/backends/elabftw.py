"""The ``elabftw://`` backend, built on ``elabapi-python``.

Install with ``pip install 'metarelay[elabftw]'``. Decisions and the spike
behind them: ``docs/adr/0001-elabftw-record-storage.md``.

Connecting
    eLabFTW uses an API key (``AuthFlow.TOKEN``)::

        config = ElabFTWConfig(token="...")   # or METARELAY_ELABFTW_TOKEN
        session = connect("elabftw://elab.example.org/experiments", config)

    The URI's path picks the kind of entry: ``/experiments`` or
    ``/resources``. Requests go to ``https://<host>/api/v2`` unless
    ``api_url`` says otherwise (the dev stack is plain ``http``).

How a ``LogicalPath`` maps onto eLabFTW
    eLabFTW has no folders, so **every container is an entry**, titled with
    the last name in its path:

    - ``ensure_container(path, holds_records=True)`` makes a records-only
      entry, like a LabArchives page: it holds records and can't contain
      containers (``ValidationError``).
    - Any other container is a folder entry. It holds its own records too,
      and child containers point to it.
    - Each entry is tagged with every prefix of its path (``Study``,
      ``Study/Cohort A``), so clicking a tag in eLabFTW works like opening a
      folder. Tags are only for people: eLabFTW ignores their case and turns
      ``|`` into a space, so names containing ``|`` are rejected.
    - MetaRelay finds entries through two read-only extra fields,
      ``MetaRelay path`` and ``MetaRelay parent``, then checks the exact
      path in ``metadata.metarelay``.

How a record is stored (ADR 0001)
    All in the container's entry, written in one request:

    - ``metadata.metarelay.records[<record_id>]``: the record JSON, the source
      of truth;
    - the body: every record in the entry rendered as tables
      (``render_preview``);
    - ``metadata.extra_fields``: the same rows as eLabFTW fields, one group
      per record and table.

    ``metadata.metarelay.preview_checksum`` is the checksum of the body and
    ``extra_fields`` as eLabFTW saved them at MetaRelay's last write. If they
    differ at the next write into the entry, someone edited it in eLabFTW:
    the write raises ``ConflictError`` until it is forced (D7). ``force``
    rebuilds the whole entry, so it also replaces hand edits to the other
    records' tables.

    Files (``write_file``) are uploads on the entry.

Limits
    - A write reads the entry, then replaces its body and metadata. eLabFTW
      has no version check, so two writers saving into the same entry at the
      same moment can lose one save. ``create`` mode is best effort.
    - Two writers creating one container can each make an entry. Lookups
      settle on the one holding records, else the oldest; two that both hold
      records are a ``ConflictError``.
    - An entry whose ``metadata.metarelay`` is broken is a ``ValidationError``,
      never treated as missing (that would make a second entry).
    - If a write fails after eLabFTW saved it but before MetaRelay stored
      the saved checksum, the next write into that entry is a false
      conflict until forced.
    - Reading costs one request per entry: eLabFTW's lists leave out
      ``metadata``.
    - Lookups only see the API key's team.
    - Nothing here deletes an entry.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import SettingsConfigDict

from metarelay.backends.elabftw_client import ElabClient, Entity
from metarelay.core.auth import AuthFlow, LoginChallenge
from metarelay.core.checksum import canonical_json, preview_checksum, record_checksum
from metarelay.core.config import DestinationConfig, TokenConfig
from metarelay.core.conflict import WriteAction, WriteMode, decide_write
from metarelay.core.destination import Destination
from metarelay.core.errors import (
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from metarelay.core.path import ROOT, Container, LogicalPath
from metarelay.core.preview import Table, render_preview
from metarelay.core.receipt import FileReceipt, Receipt
from metarelay.core.record import Record
from metarelay.core.session import StoredRecord

__all__ = ["ElabFTWBackend", "ElabFTWConfig", "ElabFTWSession"]

BACKEND_NAME = "elabftw"
FORMAT = 1
"""Version of ``metadata.metarelay``. Bump it if the layout changes."""
FOLDER, RECORDS = "folder", "records"
PATH_FIELD = "MetaRelay path"
PARENT_FIELD = "MetaRelay parent"
FILE_COMMENT = "MetaRelay file"
TAG_MAX = 255
_ENTITIES: dict[str, Entity] = {"/experiments": "experiments", "/resources": "items"}


class ElabFTWConfig(TokenConfig):
    """An eLabFTW API key.

    Reads ``METARELAY_ELABFTW_TOKEN``, ``METARELAY_ELABFTW_API_URL`` and
    ``METARELAY_ELABFTW_VERIFY_TLS`` when not passed.
    """

    model_config = SettingsConfigDict(env_prefix="METARELAY_ELABFTW_")

    api_url: str | None = None
    """The API root. Defaults to ``https://<host from the URI>/api/v2``."""
    verify_tls: bool = True
    """Check the server's certificate. Turn off only for a local test server."""

    @field_validator("api_url")
    @classmethod
    def _check_api_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("api_url must be an http:// or https:// URL with a host")
        return value.rstrip("/")


def _config(config: DestinationConfig | None) -> ElabFTWConfig:
    if config is None:
        return ElabFTWConfig()
    if not isinstance(config, ElabFTWConfig):
        raise ConfigurationError(f"elabftw:// needs an ElabFTWConfig, not {type(config).__name__}.")
    return config


def _entity(destination: Destination) -> Entity:
    if not destination.netloc:
        raise ConfigurationError(
            f"{destination.uri} has no host. Use elabftw://elab.example.org/experiments."
        )
    entity = _ENTITIES.get(destination.path)
    if entity is None or "?" in destination.uri or "#" in destination.uri:
        raise ConfigurationError(
            f"{destination.uri} must end in /experiments or /resources, for example "
            "elabftw://elab.example.org/experiments."
        )
    return entity


# Names, tags, and lookups


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _parts(path: LogicalPath) -> list[str]:
    return [_nfc(part) for part in path.parts]


def _check_names(path: LogicalPath) -> None:
    for part in path.parts:
        if "|" in part:
            raise ValidationError(
                f"Name {part!r} can't be used in eLabFTW: its tags turn '|' into a space."
            )


def _tags(path: LogicalPath) -> list[str]:
    """Every prefix of the path, for people browsing by tag.

    eLabFTW can't store a tag over 255 characters (it answers HTTP 500), so
    longer prefixes get no tag. Lookups never use tags, so nothing breaks.
    """
    parts = _parts(path)
    prefixes = ["/".join(parts[: index + 1]) for index in range(len(parts))]
    return [tag for tag in prefixes if len(tag) <= TAG_MAX]


def _literal(value: str) -> str:
    """Quote ``value`` for an extended search.

    The search uses SQL ``LIKE``, which ignores case and treats ``%`` and
    ``_`` as wildcards, so it can match too much; callers check the exact
    path afterwards. A backslash matches nothing, so it is sent as ``_``
    (any one character) to keep the search from ever matching too little.
    """
    return '"' + value.replace("\\", "_").replace('"', '\\"') + '"'


def _field_query(field: str, value: str) -> str:
    return f"extrafield:s:{_literal(field)}:{_literal(value)}"


def _check_file_name(name: str) -> str:
    """The same rules as a path name (``LogicalPath``)."""
    try:
        LogicalPath((name,))
    except ValidationError as error:
        raise ValidationError(f"File name {name!r} can't be used in eLabFTW: {error}") from None
    return _nfc(name)


# What an entry holds


def _ours(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """``metadata.metarelay``, if the entry is a MetaRelay container."""
    ours = entry["metadata"].get("metarelay")
    if not isinstance(ours, dict) or ours.get("format") != FORMAT:
        return None
    if ours.get("kind") not in {FOLDER, RECORDS} or not isinstance(ours.get("records"), dict):
        return None
    path = ours.get("path")
    if not isinstance(path, list) or not path or not all(isinstance(p, str) for p in path):
        return None
    return ours


def _path_field(entry: Mapping[str, Any]) -> str | None:
    """The value of the entry's ``MetaRelay path`` field, if it has one."""
    fields = entry["metadata"].get("extra_fields")
    field = fields.get(PATH_FIELD) if isinstance(fields, dict) else None
    value = field.get("value") if isinstance(field, dict) else None
    return _nfc(value) if isinstance(value, str) else None


def _entry_checksum(body: str | None, extra_fields: Any) -> str:
    """Fingerprint of what people see and can edit: the body and the fields."""
    shown = canonical_json({"body": body or "", "extra_fields": extra_fields or {}})
    return preview_checksum(shown.decode())


def _saved_checksum(entry: Mapping[str, Any]) -> str:
    return _entry_checksum(entry.get("body"), entry["metadata"].get("extra_fields"))


def _records(ours: Mapping[str, Any], where: str) -> dict[str, Record]:
    records = {}
    for record_id, data in ours["records"].items():
        try:
            record = Record.model_validate(data)
        except ValidationError as error:
            raise ValidationError(
                f"{where} holds an invalid record {record_id!r}: {error}"
            ) from error
        if record.record_id != record_id:
            raise ValidationError(f"{where} stores record {record.record_id!r} as {record_id!r}.")
        records[record_id] = record
    return records


def _render(path: LogicalPath, kind: str, records: Mapping[str, Record]) -> tuple[str, dict, dict]:
    """The body, ``extra_fields``, and field groups for an entry."""
    if records:
        body = "\n<hr>\n".join(render_preview(records[key]).html for key in sorted(records))
    elif kind == FOLDER:
        body = "<p>A MetaRelay folder. Records written here appear in this entry.</p>"
    else:
        body = "<p>No MetaRelay records yet.</p>"

    groups = [{"id": 1, "name": "MetaRelay"}]
    fields: dict[str, dict[str, Any]] = {}

    def add(label: str, value: str, group_id: int, **extra: Any) -> None:
        unique, count = label, 1
        while unique in fields:
            count += 1
            unique = f"{label} ({count})"
        fields[unique] = {
            "type": "text",
            "value": value,
            "group_id": group_id,
            "position": len(fields),
            **extra,
        }

    add(PATH_FIELD, "/".join(_parts(path)), 1, readonly=True)
    add(PARENT_FIELD, "/".join(_parts(path.parent)), 1, readonly=True)
    for record_id in sorted(records):
        tables = [b for b in render_preview(records[record_id]).blocks if isinstance(b, Table)]
        keys = [key for table in tables for key, _ in table.rows]
        for table in tables:
            group_id = len(groups) + 1
            groups.append({"id": group_id, "name": f"{record_id} · {table.title or 'Fields'}"})
            for key, value in table.rows:
                # Prefix the table title only when two tables share a row name.
                label = key if keys.count(key) == 1 else f"{table.title} · {key}"
                add(f"{record_id} · {label}", value, group_id)
    return body, fields, {"extra_fields_groups": groups, "display_main_text": True}


def _metadata(
    previous: Mapping[str, Any],
    path: LogicalPath,
    kind: str,
    records: Mapping[str, Record],
) -> tuple[str, dict[str, Any]]:
    """The entry's new body and metadata, keeping keys MetaRelay doesn't own."""
    body, fields, elabftw = _render(path, kind, records)
    metadata = {k: v for k, v in previous.items() if k not in {"metarelay", "extra_fields"}}
    metadata["elabftw"] = {**previous.get("elabftw", {}), **elabftw}
    metadata["extra_fields"] = fields
    metadata["metarelay"] = {
        "format": FORMAT,
        "kind": kind,
        "path": _parts(path),
        "records": {key: records[key].model_dump(mode="json") for key in sorted(records)},
        "preview_checksum": _entry_checksum(body, fields),
    }
    return body, metadata


class ElabFTWSession:
    """An open connection to one eLabFTW instance and entry type."""

    def __init__(self, client: ElabClient, destination: str) -> None:
        self._client = client
        self._destination = destination
        self._closed = False

    @property
    def destination(self) -> str:
        return self._destination

    # Containers

    def list_containers(self, parent: LogicalPath = ROOT) -> list[Container]:
        _check_names(parent)
        if parent != ROOT:
            entry, ours = self._require(parent)
            if ours["kind"] == RECORDS:
                return []
        wanted = _parts(parent)
        children = []
        for entry, ours in self._search(PARENT_FIELD, "/".join(wanted)):
            if ours is None:
                continue  # unreadable entries are reported by _find, not hidden in it
            if ours["path"][:-1] == wanted and len(ours["path"]) == len(wanted) + 1:
                children.append(self._container(LogicalPath(tuple(ours["path"])), entry, ours))
        return sorted(children, key=lambda container: container.path.parts)

    def ensure_container(self, path: LogicalPath, *, holds_records: bool = False) -> Container:
        _check_names(path)
        if path == ROOT:
            return Container(path=ROOT)
        found: tuple[dict[str, Any], dict[str, Any]] | None = None
        for depth in range(1, len(path.parts) + 1):
            here = LogicalPath(path.parts[:depth])
            if found is not None and found[1]["kind"] == RECORDS:
                raise ValidationError(
                    f"{str(here.parent)!r} holds records only, so it can't contain {here.name!r}."
                )
            found = self._find(here)
            if found is None:
                kind = RECORDS if holds_records and depth == len(path.parts) else FOLDER
                found = self._create(here, kind)
        entry, ours = found
        return self._container(path, entry, ours)

    # Records

    def write_record(
        self,
        path: LogicalPath,
        record: Record,
        *,
        mode: WriteMode = WriteMode.UPSERT,
        prior_receipt: Receipt | None = None,
    ) -> Receipt:
        if path == ROOT:
            raise ValidationError("Records go inside a container, not at the root.")
        if prior_receipt is not None and prior_receipt.record_id != record.record_id:
            raise ValidationError(
                f"prior_receipt is for {prior_receipt.record_id!r}, not {record.record_id!r}."
            )
        entry, ours = self._require(path)
        records = _records(ours, self._describe(path))
        remote = records.get(record.record_id)
        action = decide_write(  # also rejects an unknown mode
            mode=mode,
            local_checksum=record_checksum(record),
            remote_checksum=record_checksum(remote) if remote is not None else None,
            prior_receipt=prior_receipt,
        )
        edited = _saved_checksum(entry) != ours.get("preview_checksum")
        forced = WriteMode(mode) is WriteMode.FORCE
        # Like decide_write: identical content with no receipt is not a conflict.
        if edited and not forced and (action is WriteAction.WRITE or prior_receipt is not None):
            raise ConflictError(
                f"The eLabFTW entry for {self._describe(path)} was edited directly since "
                "MetaRelay last wrote it. To keep the edit, copy it into the record "
                "first. Then write again with mode='force', which rebuilds the whole "
                "entry, including the other records' tables."
            )
        if action is WriteAction.WRITE:
            records[record.record_id] = record
            entry, ours = self._save(entry, path, ours["kind"], records)
        return self._receipt(path, entry, ours, records[record.record_id])

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        missing = NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        if path == ROOT:
            raise missing
        entry, ours = self._require(path)
        record = _records(ours, self._describe(path)).get(record_id)
        if record is None:
            raise missing
        return StoredRecord(record=record, receipt=self._receipt(path, entry, ours, record))

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        if path == ROOT:
            return []
        entry, ours = self._require(path)
        records = _records(ours, self._describe(path))
        return [
            StoredRecord(record=record, receipt=self._receipt(path, entry, ours, record))
            for record in (records[key] for key in sorted(records))
        ]

    def write_file(
        self,
        path: LogicalPath,
        name: str,
        data: bytes,
        *,
        media_type: str | None = None,
    ) -> FileReceipt:
        name = _check_file_name(name)
        if path == ROOT:
            raise ValidationError("Files go inside a container, not at the root.")
        entry, ours = self._require(path)
        entry_id = int(entry["id"])
        old = [
            upload["id"]
            for upload in self._client.uploads(entry_id)
            if _nfc(upload.get("real_name") or "") == name and upload.get("comment") == FILE_COMMENT
        ]
        # Upload first, then remove the old copy, so the file is never missing.
        upload_id = self._client.upload(entry_id, name, data, FILE_COMMENT)
        for old_id in old:
            self._client.delete_upload(entry_id, int(old_id))
        return FileReceipt(
            destination=self.destination,
            backend=BACKEND_NAME,
            container=self._container(path, entry, ours),
            name=name,
            checksum=hashlib.sha256(data).hexdigest(),
            size=len(data),
            native_id=str(upload_id),
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._client.close()

    # Finding and saving entries

    def _describe(self, path: LogicalPath) -> str:
        return repr(str(path)) if path != ROOT else "the root"

    def _search(self, field: str, value: str) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        """Entries whose ``field`` may equal ``value``, with ``metadata.metarelay``
        (``None`` if it is missing or unreadable)."""
        results = []
        for summary in self._client.search(_field_query(field, value)):
            try:
                entry = self._client.get(int(summary["id"]))
            except NotFoundError:
                continue  # deleted since the search
            results.append((entry, _ours(entry)))
        return results

    def _find(self, path: LogicalPath) -> tuple[dict[str, Any], dict[str, Any]] | None:
        wanted = _parts(path)
        matches, unreadable = [], []
        for entry, ours in self._search(PATH_FIELD, "/".join(wanted)):
            if ours is not None:
                if ours["path"] == wanted:
                    matches.append((entry, ours))
            elif _path_field(entry) == "/".join(wanted):
                unreadable.append(str(entry["id"]))
        if unreadable:
            # Treating it as missing would make a second entry for this path.
            raise ValidationError(
                f"eLabFTW entry {', '.join(unreadable)} is {self._describe(path)}, but its "
                "MetaRelay metadata (metadata.metarelay) is missing or unreadable. "
                "Restore it, or remove the entry, in eLabFTW."
            )
        if len(matches) > 1:
            # Two writers made this container at once. Settle on the one that
            # holds records, else the oldest, as the LabArchives backend does.
            with_records = [match for match in matches if match[1]["records"]]
            if len(with_records) > 1:
                ids = ", ".join(str(entry["id"]) for entry, _ in with_records)
                raise ConflictError(
                    f"More than one eLabFTW entry holds records for {self._describe(path)} "
                    f"(IDs {ids}). Move the records into one entry in eLabFTW first."
                )
            matches = with_records or sorted(matches, key=lambda match: int(match[0]["id"]))
        return matches[0] if matches else None

    def _require(self, path: LogicalPath) -> tuple[dict[str, Any], dict[str, Any]]:
        _check_names(path)
        found = self._find(path)
        if found is None:
            raise NotFoundError(f"No container {self._describe(path)}.")
        return found

    def _create(self, path: LogicalPath, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
        body, metadata = _metadata({}, path, kind, {})
        entry_id = self._client.create(_parts(path)[-1], _tags(path), body, metadata)
        created = self._settle(entry_id, metadata)
        # Another writer may have made it at the same moment: settle on one.
        return self._find(path) or created

    def _save(
        self,
        entry: Mapping[str, Any],
        path: LogicalPath,
        kind: str,
        records: Mapping[str, Record],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        body, metadata = _metadata(entry["metadata"], path, kind, records)
        self._client.update(int(entry["id"]), body=body, metadata=metadata)
        return self._settle(int(entry["id"]), metadata)

    def _settle(
        self, entry_id: int, metadata: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Record the checksum of the entry as eLabFTW saved it.

        eLabFTW cleans up the HTML it is sent, so the saved body can differ
        from MetaRelay's. Comparing later against what MetaRelay sent would
        report a conflict that nobody caused.
        """
        entry = self._client.get(entry_id)
        saved = _saved_checksum(entry)
        if saved != metadata["metarelay"]["preview_checksum"]:
            metadata["metarelay"]["preview_checksum"] = saved
            self._client.update(entry_id, body=entry.get("body") or "", metadata=metadata)
            entry = self._client.get(entry_id)
        ours = _ours(entry)
        if ours is None:
            raise ValidationError(f"eLabFTW did not keep MetaRelay's metadata on entry {entry_id}.")
        return entry, ours

    def _container(
        self, path: LogicalPath, entry: Mapping[str, Any], ours: Mapping[str, Any]
    ) -> Container:
        holds_records = ours["kind"] == RECORDS
        return Container(path=path, native_id=str(entry["id"]), holds_records=holds_records)

    def _receipt(
        self,
        path: LogicalPath,
        entry: Mapping[str, Any],
        ours: Mapping[str, Any],
        record: Record,
    ) -> Receipt:
        checksum = record_checksum(record)
        return Receipt(
            destination=self.destination,
            backend=BACKEND_NAME,
            container=self._container(path, entry, ours),
            record_id=record.record_id,
            schema_id=record.schema_id,
            schema_version=record.schema_version,
            checksum=checksum,
            conflict_token=checksum,
            # As MetaRelay last wrote the entry, so a hand edit still conflicts
            # after a read (D7).
            preview_checksum=ours.get("preview_checksum"),
            backend_ids={"entry_id": str(entry["id"]), "entity": self._client.entity},
        )


class ElabFTWBackend:
    """The ``elabftw://`` backend. Registered under ``metarelay.backends``."""

    name = BACKEND_NAME
    auth_flow = AuthFlow.TOKEN

    def open(self, destination: Destination, config: DestinationConfig | None) -> ElabFTWSession:
        entity = _entity(destination)
        settings = _config(config)
        api_url = settings.api_url or f"https://{destination.netloc}/api/v2"
        client = ElabClient(
            api_url,
            settings.token.get_secret_value(),
            entity=entity,
            verify_tls=settings.verify_tls,
        )
        try:
            client.check_login()
        except BaseException:
            client.close()
            raise
        return ElabFTWSession(client, destination.uri)

    def begin_login(
        self, destination: Destination, redirect_url: str, config: DestinationConfig | None
    ) -> LoginChallenge:
        raise ConfigurationError("elabftw:// uses an API key. Use connect() with an ElabFTWConfig.")

    def complete_login(
        self,
        challenge: LoginChallenge,
        params: Mapping[str, str],
        config: DestinationConfig | None,
    ) -> ElabFTWSession:
        raise ConfigurationError("elabftw:// uses an API key. Use connect() with an ElabFTWConfig.")
