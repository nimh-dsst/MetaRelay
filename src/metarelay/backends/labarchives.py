"""The ``labarchives://`` backend, built on ``labapi``.

Install with ``pip install 'metarelay[labarchives]'``.

Logging in
    LabArchives uses a browser login (``AuthFlow.REDIRECT``)::

        config = LabArchivesConfig(akid="...", akpass="...")   # or METARELAY_LABARCHIVES_*
        challenge = begin_login("labarchives://api.labarchives.com", callback_url, config)
        # send the user to challenge.auth_url; LabArchives redirects to
        # callback_url?email=...&auth_code=...
        session = complete_login(challenge, callback_query_params, config)

    ``akid``/``akpass`` are the institution's API key pair. Requests go to
    ``https://<host>`` unless ``api_url`` says otherwise (the fake server in
    ``metarelay.testing.labarchives_fake`` is plain ``http``).

How a ``LogicalPath`` maps onto LabArchives
    - The first name is a **notebook**. ``list_containers(ROOT)`` lists the
      user's notebooks. MetaRelay never creates one: a missing notebook is
      ``NotFoundError``, so a typo doesn't make a stray notebook.
    - Names in the middle are **folders**.
    - The last name is a folder or a **page**. ``ensure_container`` creates a
      folder, or a page when called with ``holds_records=True``. Anything
      already there is used as it is. If a folder and a page share the name,
      records (``write_record``, ``read_record``, ``list_records``,
      ``write_file``) use the page and container operations use the folder,
      as Chronoxiv does. A page can't contain anything, so creating a
      container inside one raises ``ValidationError``. Two folders (or two
      pages) with the same name are ambiguous: ``ConflictError``.
    - Records and files in a page go on that page. Records in a folder (or a
      notebook) go on a page named ``MetaRelay records`` inside it, and files
      (``write_file``) on a page named ``MetaRelay files``. Those two pages
      are created on the first write and are not listed as containers. Other
      entries on any page are left alone.

How a record is stored (plan §3.3)
    Two entries on the records page:

    - a JSON **attachment**, ``<record_id>.json``, captioned
      ``MetaRelay record | <record_id> | preview <entry ID> | checksum <sha256>``.
      It is the source of truth. Its caption names the preview entry and
      holds the preview's checksum as LabArchives saved it at MetaRelay's last
      write, so a receipt from a read still catches a later hand edit (D7);
    - a rich-text **preview** entry: the marker
      ``<!-- metarelay-record:<record_id> -->`` followed by the record
      rendered as HTML tables (``render_preview``).

    The preview is found through the caption, so it is still found if a
    person editing it deletes the marker, or LabArchives strips HTML comments.
    The marker is the fallback, for a preview whose attachment was never
    written (a write that failed halfway) or a caption that names an entry no
    longer on the page.

    The conflict token is the SHA-256 of the attachment bytes. The receipt's
    ``preview_checksum`` covers the preview entry, so an edit made in
    LabArchives is reported as a conflict instead of overwritten (D7).

Limits
    - ``create`` mode is best effort: LabArchives can only check, then write.
    - ``labapi`` cannot delete entries, so nothing here deletes anything.
    - Notebooks are listed as they were at login. A notebook created
      elsewhere appears after the next login.
"""

from __future__ import annotations

import contextlib
import hashlib
import mimetypes
import unicodedata
from collections.abc import Iterator, Mapping
from io import BytesIO
from typing import Any
from urllib.parse import urlsplit

import labapi
import requests
from lxml.etree import XMLSyntaxError
from pydantic import SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

from metarelay.core.auth import AuthFlow, LoginChallenge
from metarelay.core.checksum import preview_checksum, record_checksum
from metarelay.core.config import DestinationConfig
from metarelay.core.conflict import WriteAction, WriteMode, decide_write
from metarelay.core.destination import Destination
from metarelay.core.errors import (
    AuthenticationError,
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

__all__ = [
    "FILES_PAGE",
    "RECORDS_PAGE",
    "LabArchivesBackend",
    "LabArchivesConfig",
    "LabArchivesSession",
]

BACKEND_NAME = "labarchives"
RECORDS_PAGE = "MetaRelay records"
FILES_PAGE = "MetaRelay files"
RESERVED_PAGES = frozenset({RECORDS_PAGE, FILES_PAGE})
RECORD_CAPTION = "MetaRelay record | "
FILE_CAPTION = "MetaRelay file | "
JSON_TYPE = "application/json"


def _marker(record_id: str) -> str:
    # Record IDs are letters, digits, ".", "_" and "-", so they can't end the comment.
    return f"<!-- metarelay-record:{record_id} -->"


def _record_caption(record_id: str, preview_id: str, written_checksum: str) -> str:
    return f"{RECORD_CAPTION}{record_id} | preview {preview_id} | checksum {written_checksum}"


def _caption_parts(caption: str) -> tuple[str, str | None, str | None] | None:
    """The record ID, preview entry ID, and written preview checksum in a caption.

    ``None`` if it isn't a record caption. Captions written by earlier
    versions have no preview ID or checksum (``None``). Record IDs can't
    contain spaces, so the `` | `` separator can't be part of one.
    """
    if not caption.startswith(RECORD_CAPTION):
        return None
    record_id, *fields = caption.removeprefix(RECORD_CAPTION).split(" | ")
    values = dict(field.split(" ", 1) for field in fields if " " in field)
    return record_id, values.get("preview"), values.get("checksum")


class LabArchivesConfig(DestinationConfig):
    """The institution's LabArchives API key pair.

    Reads ``METARELAY_LABARCHIVES_AKID``, ``METARELAY_LABARCHIVES_AKPASS``,
    and ``METARELAY_LABARCHIVES_API_URL`` when not passed.
    """

    model_config = SettingsConfigDict(env_prefix="METARELAY_LABARCHIVES_")

    akid: str
    """The access key ID."""
    akpass: SecretStr
    """The access password. Signs every request; never sent itself."""
    api_url: str | None = None
    """Where requests go. Defaults to ``https://<host from the URI>``."""

    @field_validator("api_url")
    @classmethod
    def _check_api_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("api_url must be an http:// or https:// URL with a host")
        return value.rstrip("/")


def _config(config: DestinationConfig | None) -> LabArchivesConfig:
    if config is None:
        return LabArchivesConfig()
    if not isinstance(config, LabArchivesConfig):
        raise ConfigurationError(
            f"labarchives:// needs a LabArchivesConfig, not {type(config).__name__}."
        )
    return config


def _check_destination(destination: Destination) -> None:
    if not destination.netloc:
        raise ConfigurationError(
            f"{destination.uri} has no host. Use labarchives://api.labarchives.com."
        )
    if destination.path or "?" in destination.uri or "#" in destination.uri:
        raise ConfigurationError(
            f"{destination.uri} has a path, query, or fragment. Name only the host, "
            "for example labarchives://api.labarchives.com. Notebooks are the first "
            "name in each path."
        )


# LabArchives error codes that mean more than "could not". Codes labapi turns
# into labapi.AuthenticationError (4506, 4514, 4520, 4533) never get here.
# LabArchives' not-found codes aren't documented anywhere we can read; add them
# here once the smoke script's deliberate bad-ID call captures a real one.
_ERROR_CODES: dict[int, type[MetaRelayError]] = {
    4547: PermissionDeniedError,  # not the notebook owner (documented by labapi)
}


@contextlib.contextmanager
def _labarchives_errors(action: str) -> Iterator[None]:
    """Translate ``labapi`` and network errors into metarelay errors.

    ``labapi``'s messages can include the signed request URL, so the
    original exception is never chained or quoted unless LabArchives sent an
    error code (then the message is just the code and description).
    """
    try:
        yield
    except MetaRelayError:
        raise
    except labapi.AuthenticationError as error:
        detail = f": {error}" if error.error_code is not None else "."
        raise AuthenticationError(
            f"LabArchives rejected the credentials while trying to {action}{detail}"
        ) from None
    except labapi.ApiError as error:
        detail = f": {error}" if error.error_code is not None else "."
        error_class = _ERROR_CODES.get(error.error_code, BackendUnavailableError)
        raise error_class(f"LabArchives could not {action}{detail}") from None
    except (labapi.LabArchivesError, XMLSyntaxError):
        raise BackendUnavailableError(
            f"LabArchives sent a response MetaRelay did not understand while trying to {action}."
        ) from None
    except requests.RequestException as error:
        raise BackendUnavailableError(
            f"Could not reach LabArchives to {action} ({type(error).__name__})."
        ) from None


def _is_container(node: Any) -> bool:
    """Folders, and pages other than a folder's own records and files pages."""
    return node.is_dir() or node.name not in RESERVED_PAGES


def _escape_name(name: str) -> str:
    r"""``name`` as labapi needs it to create a node with exactly that name.

    labapi 1.2 reads ``\`` and ``/`` in a name as path escapes, so they are
    escaped; labapi 1.0.3 has no escapes and takes the name as it is.
    """
    escape = getattr(labapi.NotebookPath, "escape", None)
    return escape(name)[0] if escape is not None else name


def _addressable(name: str) -> bool:
    """Whether ``name`` can be part of a LogicalPath.

    LabArchives allows names MetaRelay can't address, like a dated folder
    ``11/12/2026``. Listings skip them instead of failing on them.
    """
    try:
        LogicalPath((name,))
    except ValidationError:
        return False
    return True


def _same_name(stored: str, wanted: str) -> bool:
    """Whether a LabArchives name is ``wanted``, ignoring Unicode spelling.

    ``ä`` can be stored as one character or as ``a`` plus a combining mark;
    the local backend treats both as one name, and so does this one.
    """
    return unicodedata.normalize("NFC", stored) == unicodedata.normalize("NFC", wanted)


def _container(path: LogicalPath, node: Any) -> Container:
    return Container(path=path, native_id=node.id, holds_records=not node.is_dir())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download(entry: Any) -> bytes:
    attachment = entry.content
    attachment.seek(0)
    return attachment.read()


def _check_file_name(name: str) -> str:
    if (
        not name.strip()
        or name != name.strip()
        or "/" in name
        or any(unicodedata.category(char) == "Cc" for char in name)
    ):
        raise ValidationError(f"File name {name!r} can't be used in LabArchives.")
    return name


class LabArchivesSession:
    """A logged-in LabArchives user. Made by ``complete_login``."""

    def __init__(self, client: labapi.Client, user: labapi.User, destination: str) -> None:
        self._client = client
        self._user = user
        self._destination = destination
        self._closed = False

    @property
    def destination(self) -> str:
        return self._destination

    def list_containers(self, parent: LogicalPath = ROOT) -> list[Container]:
        with _labarchives_errors(f"list {self._describe(parent)}"):
            if parent == ROOT:
                return [
                    Container(path=LogicalPath((notebook.name,)), native_id=notebook.id)
                    for notebook in self._user.notebooks.all_values()
                    if _addressable(notebook.name)
                ]
            _, node = self._walk(parent, create=False)
            if not node.is_dir():
                return []
            return [
                _container(parent / child.name, child)
                for child in node.children
                if _is_container(child) and _addressable(child.name)
            ]

    def ensure_container(self, path: LogicalPath, *, holds_records: bool = False) -> Container:
        if path == ROOT:
            return Container(path=ROOT)
        page_wanted = holds_records and len(path.parts) > 1  # a notebook is never a page
        if page_wanted and path.name in RESERVED_PAGES:
            raise ValidationError(
                f"{path.name!r} is the name of the page where a folder keeps its "
                f"{'records' if path.name == RECORDS_PAGE else 'files'}. Choose another name."
            )
        with _labarchives_errors(f"create {self._describe(path)}"):
            _, node = self._walk(path, create=True, prefer_page=page_wanted)
            return _container(path, node)

    def write_record(
        self,
        path: LogicalPath,
        record: Record,
        *,
        mode: WriteMode = WriteMode.UPSERT,
        prior_receipt: Receipt | None = None,
    ) -> Receipt:
        if path == ROOT:
            raise ValidationError("Records go inside a notebook, not at the root.")
        if prior_receipt is not None and prior_receipt.record_id != record.record_id:
            # Its preview ID would point this write at the other record's preview.
            raise ValidationError(
                f"prior_receipt is for {prior_receipt.record_id!r}, not {record.record_id!r}."
            )
        where = f"write {record.record_id!r} in {self._describe(path)}"
        with _labarchives_errors(where):
            notebook, node = self._walk(path, create=False, prefer_page=True)
            page = self._records_page(node, create=False)
            # The receipt's preview ID is a second way to find the preview, after
            # the caption: a person editing it may have removed the marker.
            known_preview = (
                prior_receipt.backend_ids.get("preview_entry_id") if prior_receipt else None
            )
            attachment_entry, preview_entry, _ = (
                self._find(page, record.record_id, known_preview) if page else (None, None, None)
            )
            remote_checksum = remote_bytes = None
            if attachment_entry is not None:
                remote_bytes = _download(attachment_entry)
                remote_checksum = self._checksum_of(remote_bytes)
            action = decide_write(  # also rejects an unknown mode
                mode=mode,
                local_checksum=record_checksum(record),
                remote_checksum=remote_checksum,
                prior_receipt=prior_receipt,
                remote_preview_checksum=(
                    preview_checksum(preview_entry.content) if preview_entry else None
                ),
            )

            html = _marker(record.record_id) + render_preview(record).html
            if page is None:
                page = self._records_page(node, create=True)
            filename = f"{record.record_id}.json"
            if action is WriteAction.WRITE:
                # The preview goes first: the attachment's caption names it and
                # records its checksum as LabArchives saved it. If the
                # attachment then fails, the next write finds this preview by
                # its marker; its checksum no longer matches the caption, so
                # that write reports a conflict until it is forced.
                if preview_entry is None:
                    preview_entry = page.entries.create(labapi.TextEntry, html)
                elif preview_entry.content != html:
                    preview_entry.content = html
                written = self._saved_checksum(page, preview_entry.id)
                data = (record.model_dump_json(indent=2) + "\n").encode()
                caption = _record_caption(record.record_id, preview_entry.id, written)
                attachment = labapi.Attachment(BytesIO(data), JSON_TYPE, filename, caption)
                if attachment_entry is not None:
                    attachment_entry.content = attachment
                else:
                    page.entries.create(labapi.AttachmentEntry, attachment)
            elif preview_entry is None:
                # Same content: leave it alone, but restore a missing preview
                # and point the caption at it (the same bytes, re-uploaded).
                # An edited preview is not touched.
                preview_entry = page.entries.create(labapi.TextEntry, html)
                written = self._saved_checksum(page, preview_entry.id)
                caption = _record_caption(record.record_id, preview_entry.id, written)
                attachment_entry.content = labapi.Attachment(
                    BytesIO(remote_bytes), JSON_TYPE, filename, caption
                )

            page.refresh()
            return self._read(path, notebook, node, page, record.record_id).receipt

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        missing = NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        if path == ROOT:
            raise missing
        with _labarchives_errors(f"read {record_id!r} in {self._describe(path)}"):
            notebook, node = self._walk(path, create=False, prefer_page=True)
            page = self._records_page(node, create=False)
            if page is None:
                raise missing
            return self._read(path, notebook, node, page, record_id)

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        if path == ROOT:
            return []
        with _labarchives_errors(f"list the records in {self._describe(path)}"):
            notebook, node = self._walk(path, create=False, prefer_page=True)
            page = self._records_page(node, create=False)
            if page is None:
                return []
            ids = {
                parts[0]
                for entry in page.entries
                if isinstance(entry, labapi.AttachmentEntry)
                and (parts := _caption_parts(entry.caption)) is not None
            }
            return [self._read(path, notebook, node, page, record_id) for record_id in sorted(ids)]

    def write_file(
        self,
        path: LogicalPath,
        name: str,
        data: bytes,
        *,
        media_type: str | None = None,
    ) -> FileReceipt:
        _check_file_name(name)
        if path == ROOT:
            raise ValidationError("Files go inside a notebook, not at the root.")
        with _labarchives_errors(f"write {name!r} in {self._describe(path)}"):
            _, node = self._walk(path, create=False, prefer_page=True)
            page = self._page(node, FILES_PAGE, create=True) if node.is_dir() else node
            caption = FILE_CAPTION + name
            matches = [
                entry
                for entry in page.entries
                if isinstance(entry, labapi.AttachmentEntry) and entry.caption == caption
            ]
            if len(matches) > 1:
                raise ConflictError(
                    f"The {page.name!r} page in {self._describe(path)} has more than one "
                    f"{name!r}. Remove the extra entries in LabArchives first."
                )
            content_type = media_type or mimetypes.guess_type(name)[0]
            attachment = labapi.Attachment(
                BytesIO(data), content_type or "application/octet-stream", name, caption
            )
            if matches:
                entry = matches[0]
                entry.content = attachment
            else:
                entry = page.entries.create(labapi.AttachmentEntry, attachment)
            return FileReceipt(
                destination=self.destination,
                backend=BACKEND_NAME,
                container=_container(path, node),
                name=name,
                checksum=_sha256(data),
                size=len(data),
                native_id=entry.id,
            )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._client.close()

    # Finding things

    def _describe(self, path: LogicalPath) -> str:
        return repr(str(path)) if path != ROOT else "the notebook list"

    def _walk(
        self, path: LogicalPath, *, create: bool, prefer_page: bool = False
    ) -> tuple[Any, Any]:
        """The notebook and the notebook, folder, or page at ``path``.

        If the last name is both a folder and a page, ``prefer_page`` picks
        the page (for records) and otherwise the folder. With ``create``,
        makes missing folders, and a page for the last name if
        ``prefer_page``; never a notebook. Re-reads every level, because
        ``labapi`` caches what it has seen and someone may have changed the
        notebook since.
        """
        notebook_name, *names = path.parts
        notebooks = [
            nb for nb in self._user.notebooks.all_values() if _same_name(nb.name, notebook_name)
        ]
        if len(notebooks) > 1:
            raise ConflictError(
                f"You have more than one notebook named {notebook_name!r}. "
                "Rename one in LabArchives first."
            )
        if not notebooks:
            raise NotFoundError(
                f"No notebook named {notebook_name!r}. Create it in LabArchives first; "
                "MetaRelay doesn't create notebooks."
            )
        notebook = notebooks[0]
        notebook.refresh()

        node = notebook
        for depth, name in enumerate(names, start=2):
            here = LogicalPath(path.parts[:depth])
            last = depth == len(path.parts)
            named = [child for child in node.children if _same_name(child.name, name)]
            dirs = [child for child in named if child.is_dir()]
            pages = [child for child in named if _is_container(child) and not child.is_dir()]
            # Middle names can only be folders. For the last name, records use a
            # page and everything else a folder, when both exist.
            use_pages = bool(pages) and (not dirs or (last and prefer_page))
            matches, kind = (pages, "page") if use_pages else (dirs, "folder")
            if len(matches) > 1:
                raise ConflictError(
                    f"{str(here.parent)!r} has more than one {kind} named {name!r}. "
                    "Rename one in LabArchives first."
                )
            if matches and (last or not use_pages):
                node = matches[0]
            elif matches:
                child = path.parts[depth]
                if create:
                    raise ValidationError(
                        f"{str(here)!r} is a page, which holds records only, so it can't "
                        f"contain {child!r}."
                    )
                raise NotFoundError(
                    f"Container {str(path)!r} does not exist: {str(here)!r} is a page."
                )
            elif create:
                escaped = _escape_name(name)
                page = last and prefer_page
                node = node.create(
                    labapi.NotebookPage if page else labapi.NotebookDirectory, escaped
                )
            else:
                raise NotFoundError(f"Container {str(here)!r} does not exist. Create it first.")
        return notebook, node

    def _records_page(self, node: Any, *, create: bool) -> Any | None:
        """The page that holds records for the container ``node``."""
        if not node.is_dir():
            return node
        return self._page(node, RECORDS_PAGE, create=create)

    def _page(self, node: Any, name: str, *, create: bool) -> Any | None:
        """The folder's own page called ``name`` (records or files).

        Two writers can each find no page and each make one. So after making
        it, read the folder again, and when there are several, every writer
        settles on the same one: the one with entries, or else the lowest ID.
        The extra page stays, empty; labapi can't delete a page, only move it
        to a visible "API Deleted Items" folder.
        """
        pages = self._pages_named(node, name)
        if not pages and create:
            node.create(labapi.NotebookPage, name)
            node.refresh()
            pages = self._pages_named(node, name)
        if len(pages) <= 1:
            return pages[0] if pages else None
        in_use = [page for page in pages if len(page.entries)]
        if len(in_use) > 1:
            raise ConflictError(
                f"More than one page named {name!r} in {node.name!r} has entries. "
                "Move them onto one page in LabArchives first."
            )
        return in_use[0] if in_use else min(pages, key=lambda page: page.id)

    @staticmethod
    def _pages_named(node: Any, name: str) -> list[Any]:
        return [child for child in node.children if not child.is_dir() and child.name == name]

    def _saved_checksum(self, page: Any, entry_id: str) -> str:
        """``preview_checksum`` of a text entry as LabArchives saved it."""
        page.refresh()
        found = [entry for entry in page.entries if entry.id == entry_id]
        if len(found) != 1:
            raise BackendUnavailableError(
                f"LabArchives did not return entry {entry_id} right after saving it. "
                "Try again; if MetaRelay then reports an edit, write with mode='force'."
            )
        return preview_checksum(found[0].content)

    def _find(
        self, page: Any, record_id: str, preview_id: str | None = None
    ) -> tuple[Any | None, Any | None, str | None]:
        """The record's attachment entry, preview entry, and written checksum.

        The preview is the text entry the attachment's caption names, then the
        one ``preview_id`` (from a receipt) names, then the one that starts
        with the record's marker; the first of those on the page wins. The
        written checksum is the caption's, when the caption names the preview
        that was found; otherwise ``None``.
        """
        marker = _marker(record_id)
        attachments, texts, marked = [], {}, []
        for entry in page.entries:
            if isinstance(entry, labapi.AttachmentEntry):
                parts = _caption_parts(entry.caption)
                if parts is not None and parts[0] == record_id:
                    attachments.append((entry, parts[1], parts[2]))
            elif isinstance(entry, labapi.TextEntry):
                texts[entry.id] = entry
                if entry.content.startswith(marker):
                    marked.append(entry)
        _, named, written = attachments[0] if len(attachments) == 1 else (None, None, None)
        known = next((texts[eid] for eid in (named, preview_id) if eid in texts), None)
        previews = [known] if known is not None else marked
        if len(attachments) > 1 or len(previews) > 1:
            raise ConflictError(
                f"The {page.name!r} page has more than one entry for record {record_id!r}. "
                "Remove the extra entries in LabArchives first."
            )
        preview = previews[0] if previews else None
        if preview is None or preview.id != named:
            written = None
        return (attachments[0][0] if attachments else None, preview, written)

    def _checksum_of(self, data: bytes) -> str:
        """``record_checksum`` of an attachment, or a value that matches nothing."""
        try:
            return record_checksum(Record.model_validate_json(data))
        except ValidationError:
            return f"unreadable:{_sha256(data)}"

    def _read(
        self, path: LogicalPath, notebook: Any, node: Any, page: Any, record_id: str
    ) -> StoredRecord:
        attachment_entry, preview_entry, written = self._find(page, record_id)
        if attachment_entry is None:
            raise NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        data = _download(attachment_entry)
        where = f"The attachment for {record_id!r} in {self._describe(path)}"
        try:
            record = Record.model_validate_json(data)
        except ValidationError as error:
            raise ValidationError(f"{where} is not a valid record: {error}") from error
        if record.record_id != record_id:
            raise ValidationError(f"{where} holds record {record.record_id!r}.")
        backend_ids = {
            "notebook_id": notebook.id,
            "page_id": page.id,
            "attachment_entry_id": attachment_entry.id,
        }
        if preview_entry is not None:
            backend_ids["preview_entry_id"] = preview_entry.id
        receipt = Receipt(
            destination=self.destination,
            backend=BACKEND_NAME,
            container=_container(path, node),
            record_id=record.record_id,
            schema_id=record.schema_id,
            schema_version=record.schema_version,
            checksum=record_checksum(record),
            conflict_token=_sha256(data),
            # As MetaRelay last wrote it, so a hand edit still conflicts after a
            # read. If the caption names another entry, use the current one.
            preview_checksum=written
            or (preview_checksum(preview_entry.content) if preview_entry else None),
            backend_ids=backend_ids,
        )
        return StoredRecord(record=record, receipt=receipt)


class LabArchivesBackend:
    """The ``labarchives://`` backend. Registered under ``metarelay.backends``."""

    name = BACKEND_NAME
    auth_flow = AuthFlow.REDIRECT

    def open(self, destination: Destination, config: DestinationConfig | None) -> Any:
        raise ConfigurationError(
            "labarchives:// needs a browser login. Use begin_login() and complete_login()."
        )

    def begin_login(
        self, destination: Destination, redirect_url: str, config: DestinationConfig | None
    ) -> LoginChallenge:
        _check_destination(destination)
        client = self._client(destination, _config(config))
        try:
            with _labarchives_errors("start the login"):
                auth_url = client.generate_auth_url(redirect_url)
        finally:
            client.close()
        return LoginChallenge(
            destination=destination.uri, auth_url=auth_url, redirect_url=redirect_url
        )

    def complete_login(
        self,
        challenge: LoginChallenge,
        params: Mapping[str, str],
        config: DestinationConfig | None,
    ) -> LabArchivesSession:
        destination = Destination.parse(challenge.destination)
        _check_destination(destination)
        if params.get("error"):
            raise AuthenticationError(f"LabArchives login failed: {params['error']}")
        email, auth_code = params.get("email"), params.get("auth_code")
        if not email or not auth_code:
            raise AuthenticationError(
                "The LabArchives login response is incomplete: it needs both "
                "'email' and 'auth_code'. Log in again."
            )
        client = self._client(destination, _config(config))
        try:
            with _labarchives_errors("log in"):
                user = client.login(email, auth_code)
        except BaseException:
            client.close()
            raise
        return LabArchivesSession(client, user, destination.uri)

    def _client(self, destination: Destination, config: LabArchivesConfig) -> labapi.Client:
        api_url = config.api_url or f"https://{destination.netloc}"
        with _labarchives_errors("connect"):
            return labapi.Client(api_url, config.akid, config.akpass.get_secret_value())
