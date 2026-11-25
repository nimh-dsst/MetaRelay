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

How a ``LogicalPath`` maps onto eLabFTW (ADR 0002)
    - The first name is a **category**, like a LabArchives notebook.
      ``list_containers(ROOT)`` lists the team's categories. MetaRelay never
      creates one: a missing category is ``NotFoundError``. Records go in
      entries inside a category, not in the category itself.
    - eLabFTW has no folders, so every container below a category is an
      **entry** in that category, titled with the last name in its path.
    - ``ensure_container(path, holds_records=True)`` makes a records-only
      entry, like a LabArchives page: it holds records and can't contain
      containers (``ValidationError``).
    - Any other container is a folder entry. It holds its own records too,
      and child containers point to it.
    - MetaRelay finds entries through two read-only extra fields,
      ``MetaRelay path`` and ``MetaRelay parent``, then checks the exact
      path in ``metadata.metarelay``. Moving an entry to another category
      in eLabFTW doesn't change where MetaRelay finds it.
    - Tags are the kind of record: a record write adds the tag that
      ``ElabFTWConfig.record_tags`` maps its ``schema_id`` to. MetaRelay
      never removes a tag.

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

import contextlib
import copy
import hashlib
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator
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
from metarelay.core.preview import (
    Heading,
    PreviewDiff,
    PreviewEdit,
    Table,
    diff_blocks,
    parse_html,
    render_preview,
    to_html,
)
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
    record_tags: dict[str, str] = Field(default_factory=dict)
    """The tag for each ``schema_id``. A record write tags its entry with it."""

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


# Names and lookups


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _parts(path: LogicalPath) -> list[str]:
    return [_nfc(part) for part in path.parts]


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


_Origin = tuple[str, str | None, str]
"""Where a field's value comes from: record ID, table title, row name."""


def _render(
    path: LogicalPath, kind: str, records: Mapping[str, Record]
) -> tuple[str, dict, dict, dict[str, _Origin]]:
    """The body, ``extra_fields``, field groups, and each record field's origin."""
    if records:
        body = "\n<hr>\n".join(render_preview(records[key]).html for key in sorted(records))
    elif kind == FOLDER:
        body = "<p>A MetaRelay folder. Records written here appear in this entry.</p>"
    else:
        body = "<p>No MetaRelay records yet.</p>"

    groups = [{"id": 1, "name": "MetaRelay"}]
    fields: dict[str, dict[str, Any]] = {}
    origins: dict[str, _Origin] = {}

    def add(
        label: str, value: str, group_id: int, origin: _Origin | None = None, **extra: Any
    ) -> None:
        unique, count = label, 1
        while unique in fields:
            count += 1
            unique = f"{label} ({count})"
        if origin is not None:
            origins[unique] = origin
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
                add(f"{record_id} · {label}", value, group_id, (record_id, table.title, key))
    return body, fields, {"extra_fields_groups": groups, "display_main_text": True}, origins


def _entry_diff(
    entry: Mapping[str, Any],
    ours: Mapping[str, Any],
    path: LogicalPath,
    records: Mapping[str, Record],
) -> PreviewDiff:
    """What someone changed in the entry since MetaRelay wrote it.

    Both places a value shows are read: the fields, exactly, and the body's
    tables, by their text (the editor reformats the HTML, as LabArchives
    does). Both are compared with what the stored records render to, by
    ``diff_blocks``, the rule every backend uses. The same change made in
    both places is listed once.

    The edits are complete when nothing else changed: putting the listed
    field values back gives exactly the fields MetaRelay wrote (no field
    added, renamed, or retyped, and the path fields intact), the body still
    has the same headings and tables with nothing added between them, and the
    body and the field weren't both changed, to different values. (A value
    changed in only one of the two places is an edit.)
    """
    _, written, _, origins = _render(path, ours["kind"], records)
    fields = _fields_diff(entry, written, origins)
    body = _body_diff(entry.get("body"), records)
    seen: dict[tuple[str, str | None, str, str | None], PreviewEdit] = {}
    edits: list[PreviewEdit] = []
    disagree = False
    for edit in [*fields.edits, *body.edits]:
        key = (edit.record_id, edit.table, edit.field, edit.written)
        if key in seen:
            if seen[key].current == edit.current:
                continue  # the same change in the body and the field
            disagree = True  # the body says one thing, the field another
        seen.setdefault(key, edit)
        edits.append(edit)
    return PreviewDiff(edits=edits, complete=fields.complete and body.complete and not disagree)


def _fields_diff(
    entry: Mapping[str, Any], written: Mapping[str, Any], origins: Mapping[str, _Origin]
) -> PreviewDiff:
    """The edits to the record fields. Complete when nothing else in the fields changed."""
    current = entry["metadata"].get("extra_fields") or {}
    if not isinstance(current, dict):
        return PreviewDiff(edits=[], complete=False)
    reverted = copy.deepcopy(current)
    tables: dict[str, tuple[list[Table], list[Table]]] = {}
    for label, (record_id, title, row) in origins.items():
        value = written[label]["value"]
        now = current.get(label)
        if not isinstance(now, dict):
            reverted[label] = copy.deepcopy(written[label])
        elif now.get("value") != value:
            reverted[label]["value"] = value
        old, new = tables.setdefault(record_id, ([], []))
        if not old or old[-1].title != title:
            old.append(Table(title=title, rows=[]))
            new.append(Table(title=title, rows=[]))
        old[-1].rows.append((row, value))
        if isinstance(now, dict):
            new[-1].rows.append((row, "" if now.get("value") is None else str(now["value"])))
    # eLabFTW stores the fields as written, so anything left over is another
    # change. Not ``position``: it's display order, and re-rendering from the
    # stored records (whose keys MySQL reorders) can number fields differently.
    clean = _unordered(reverted) == _unordered(written)
    diffs = [
        diff_blocks(rid, _as_shown(old), _as_shown(new), clean=clean)
        for rid, (old, new) in tables.items()
    ]
    edits = [edit for diff in diffs for edit in diff.edits]
    return PreviewDiff(edits=edits, complete=clean and all(diff.complete for diff in diffs))


def _as_shown(tables: list[Table]) -> list[Heading | Table]:
    """Field values read the way the body's are, so the two compare equal.

    The body is read through ``parse_html``, which collapses whitespace; the
    same round trip here keeps one rule for both (as ``preview_diff`` does).
    """
    return parse_html(to_html(tables))[0]


def _unordered(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        label: {k: v for k, v in field.items() if k != "position"}
        if isinstance(field, dict)
        else field
        for label, field in fields.items()
    }


def _body_diff(body: Any, records: Mapping[str, Record]) -> PreviewDiff:
    """The edits to the body's tables, read by their text, record by record."""
    if not isinstance(body, str):
        return PreviewDiff(edits=[], complete=False)
    found, clean = parse_html(body)
    # The body is each record's preview in turn. Split it by how many blocks
    # each preview has, not by headings: a schema's own renderer may use any
    # number. Blocks added or removed leave the counts off: not complete.
    diffs: list[PreviewDiff] = []
    start = 0
    for key in sorted(records):
        expected = parse_html(render_preview(records[key]).html)[0]
        section = found[start : start + len(expected)]
        start += len(expected)
        diffs.append(diff_blocks(key, expected, section, clean=clean))
    if start != len(found):
        return PreviewDiff(edits=[], complete=False)
    return PreviewDiff(
        edits=[edit for diff in diffs for edit in diff.edits],
        complete=clean and all(diff.complete for diff in diffs),
    )


def _metadata(
    previous: Mapping[str, Any],
    path: LogicalPath,
    kind: str,
    records: Mapping[str, Record],
) -> tuple[str, dict[str, Any]]:
    """The entry's new body and metadata, keeping keys MetaRelay doesn't own."""
    body, fields, elabftw, _ = _render(path, kind, records)
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

    def __init__(
        self, client: ElabClient, destination: str, record_tags: Mapping[str, str]
    ) -> None:
        self._client = client
        self._destination = destination
        self._record_tags = record_tags
        self._closed = False

    @property
    def destination(self) -> str:
        return self._destination

    # Containers

    def list_containers(self, parent: LogicalPath = ROOT) -> list[Container]:
        if parent == ROOT:
            categories = []
            for category in self._client.categories():
                # Skip titles a path can't hold, like "11/12/2026".
                with contextlib.suppress(ValidationError):
                    path = LogicalPath((_nfc(category["title"]),))
                    categories.append(Container(path=path, native_id=str(category["id"])))
            return sorted(categories, key=lambda container: container.path.parts)
        if len(parent.parts) == 1:
            self._category(parent)
        elif self._require(parent)[1]["kind"] == RECORDS:
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
        if path == ROOT:
            return Container(path=ROOT)
        category = self._category(path)
        if len(path.parts) == 1:
            return category
        found: tuple[dict[str, Any], dict[str, Any]] | None = None
        for depth in range(2, len(path.parts) + 1):
            here = LogicalPath(path.parts[:depth])
            if found is not None and found[1]["kind"] == RECORDS:
                raise ValidationError(
                    f"{str(here.parent)!r} holds records only, so it can't contain {here.name!r}."
                )
            found = self._find(here)
            if found is None:
                kind = RECORDS if holds_records and depth == len(path.parts) else FOLDER
                found = self._create(here, kind, int(category.native_id))
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
        if len(path.parts) < 2:
            raise ValidationError("Records go in an entry inside a category.")
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
            # One body and one set of fields for every record in the entry,
            # so a hand edit anywhere in it blocks writing any of them.
            entry_edited=_saved_checksum(entry) != ours.get("preview_checksum"),
            preview_diff=lambda: _entry_diff(entry, ours, path, records),
            record_id=record.record_id,
        )
        if action is WriteAction.WRITE:
            # Tag first: if tagging fails, nothing is saved, so a retry is
            # still a write and adds the tag.
            tag = self._record_tags.get(record.schema_id)
            if tag:
                self._client.add_tag(int(entry["id"]), tag)
            records[record.record_id] = record
            entry, ours = self._save(entry, path, ours["kind"], records)
        return self._receipt(path, entry, ours, records[record.record_id])

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        missing = NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        if len(path.parts) < 2:
            raise missing
        entry, ours = self._require(path)
        record = _records(ours, self._describe(path)).get(record_id)
        if record is None:
            raise missing
        return StoredRecord(record=record, receipt=self._receipt(path, entry, ours, record))

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        if len(path.parts) < 2:  # the root and categories hold entries, not records
            if path != ROOT:
                self._category(path)
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
        if len(path.parts) < 2:
            raise ValidationError("Files go in an entry inside a category.")
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

    def _category(self, path: LogicalPath) -> Container:
        """The team's category named by the path's first name. MetaRelay never makes one."""
        name = _parts(path)[0]
        found = [c for c in self._client.categories() if _nfc(c["title"]) == name]
        if not found:
            raise NotFoundError(
                f"No category {name!r} in eLabFTW. Create it in eLabFTW first; "
                "MetaRelay doesn't create categories."
            )
        if len(found) > 1:
            raise ConflictError(
                f"More than one eLabFTW category is named {name!r}. Rename one in eLabFTW."
            )
        return Container(path=LogicalPath(path.parts[:1]), native_id=str(found[0]["id"]))

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
        found = self._find(path)
        if found is None:
            raise NotFoundError(f"No container {self._describe(path)}.")
        return found

    def _create(
        self, path: LogicalPath, kind: str, category: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        body, metadata = _metadata({}, path, kind, {})
        entry_id = self._client.create(_parts(path)[-1], category, body, metadata)
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
        return ElabFTWSession(client, destination.uri, settings.record_tags)

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
