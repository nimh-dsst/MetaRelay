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
      user's notebooks, and ``ensure_container`` creates a missing one.
    - Every later name is a **folder**. Pages are never containers.
    - A container's records live on a page named ``Metadata`` inside it, and
      its files (``write_file``) on a page named ``Files``. Both pages are
      created on the first write, so ``ensure_container`` creates folders
      only. Other entries on those pages are left alone.

How a record is stored (plan §3.3)
    Two entries on the ``Metadata`` page:

    - a JSON **attachment**, ``<record_id>.json``, captioned
      ``MetaRelay record | <record_id>``. It is the source of truth;
    - a rich-text **preview** entry: the marker
      ``<!-- metarelay-record:<record_id> -->`` followed by the record
      rendered as HTML tables (``render_preview``).

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
RECORDS_PAGE = "Metadata"
FILES_PAGE = "Files"
RECORD_CAPTION = "MetaRelay record | "
FILE_CAPTION = "MetaRelay file | "
JSON_TYPE = "application/json"


def _marker(record_id: str) -> str:
    # Record IDs are letters, digits, ".", "_" and "-", so they can't end the comment.
    return f"<!-- metarelay-record:{record_id} -->"


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
        raise BackendUnavailableError(f"LabArchives could not {action}{detail}") from None
    except (labapi.LabArchivesError, XMLSyntaxError):
        raise BackendUnavailableError(
            f"LabArchives sent a response MetaRelay did not understand while trying to {action}."
        ) from None
    except requests.RequestException as error:
        raise BackendUnavailableError(
            f"Could not reach LabArchives to {action} ({type(error).__name__})."
        ) from None


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
                ]
            _, node = self._walk(parent, create=False)
            return [
                Container(path=parent / child.name, native_id=child.id)
                for child in node.children
                if child.is_dir()
            ]

    def ensure_container(self, path: LogicalPath) -> Container:
        if path == ROOT:
            return Container(path=ROOT)
        with _labarchives_errors(f"create {self._describe(path)}"):
            _, node = self._walk(path, create=True)
            return Container(path=path, native_id=node.id)

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
        where = f"write {record.record_id!r} in {self._describe(path)}"
        with _labarchives_errors(where):
            notebook, node = self._walk(path, create=False)
            page = self._page(node, RECORDS_PAGE, create=False)
            # Find the preview by the ID we stored: a person editing it may
            # have removed the marker, and that edit must still be caught.
            known_preview = (
                prior_receipt.backend_ids.get("preview_entry_id") if prior_receipt else None
            )
            attachment_entry, preview_entry = (
                self._find(page, record.record_id, known_preview) if page else (None, None)
            )
            remote_checksum = None
            if attachment_entry is not None:
                remote_checksum = self._checksum_of(_download(attachment_entry))
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
                page = self._page(node, RECORDS_PAGE, create=True)
            if action is WriteAction.WRITE:
                data = (record.model_dump_json(indent=2) + "\n").encode()
                attachment = labapi.Attachment(
                    BytesIO(data),
                    JSON_TYPE,
                    f"{record.record_id}.json",
                    RECORD_CAPTION + record.record_id,
                )
                if attachment_entry is not None:
                    attachment_entry.content = attachment
                else:
                    page.entries.create(labapi.AttachmentEntry, attachment)
                if preview_entry is None:
                    page.entries.create(labapi.TextEntry, html)
                elif preview_entry.content != html:
                    preview_entry.content = html
            elif preview_entry is None:
                # Same content: leave it alone, but restore a missing preview.
                # An edited preview is not touched.
                page.entries.create(labapi.TextEntry, html)

            page.refresh()
            return self._read(path, notebook, page, record.record_id).receipt

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        missing = NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        if path == ROOT:
            raise missing
        with _labarchives_errors(f"read {record_id!r} in {self._describe(path)}"):
            notebook, node = self._walk(path, create=False)
            page = self._page(node, RECORDS_PAGE, create=False)
            if page is None:
                raise missing
            return self._read(path, notebook, page, record_id)

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        if path == ROOT:
            return []
        with _labarchives_errors(f"list the records in {self._describe(path)}"):
            notebook, node = self._walk(path, create=False)
            page = self._page(node, RECORDS_PAGE, create=False)
            if page is None:
                return []
            ids = {
                entry.caption.removeprefix(RECORD_CAPTION)
                for entry in page.entries
                if isinstance(entry, labapi.AttachmentEntry)
                and entry.caption.startswith(RECORD_CAPTION)
            }
            return [self._read(path, notebook, page, record_id) for record_id in sorted(ids)]

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
            _, node = self._walk(path, create=False)
            page = self._page(node, FILES_PAGE, create=True)
            caption = FILE_CAPTION + name
            matches = [
                entry
                for entry in page.entries
                if isinstance(entry, labapi.AttachmentEntry) and entry.caption == caption
            ]
            if len(matches) > 1:
                raise ConflictError(
                    f"The {FILES_PAGE} page in {self._describe(path)} has more than one "
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
                container=Container(path=path, native_id=node.id),
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

    def _walk(self, path: LogicalPath, *, create: bool) -> tuple[Any, Any]:
        """The notebook and the folder (or notebook) at ``path``.

        Re-reads every level, because ``labapi`` caches what it has seen and
        someone may have changed the notebook since.
        """
        notebook_name, *folders = path.parts
        notebooks = [nb for nb in self._user.notebooks.all_values() if nb.name == notebook_name]
        if len(notebooks) > 1:
            raise ConflictError(
                f"You have more than one notebook named {notebook_name!r}. "
                "Rename one in LabArchives first."
            )
        if notebooks:
            notebook = notebooks[0]
        elif create:
            notebook = self._user.notebooks.create_notebook(notebook_name)
        else:
            raise NotFoundError(f"No notebook named {notebook_name!r}. Create it first.")
        notebook.refresh()

        node = notebook
        for depth, name in enumerate(folders, start=2):
            here = LogicalPath(path.parts[:depth])
            matches = [child for child in node.children if child.is_dir() and child.name == name]
            if len(matches) > 1:
                raise ConflictError(
                    f"{str(here.parent)!r} has more than one folder named {name!r}. "
                    "Rename one in LabArchives first."
                )
            if matches:
                node = matches[0]
            elif create:
                # labapi reads "\" in a name as an escape; escape it so the name survives.
                escaped = labapi.NotebookPath.escape(name)[0]
                node = node.create(labapi.NotebookDirectory, escaped)
            else:
                raise NotFoundError(f"Container {str(here)!r} does not exist. Create it first.")
        return notebook, node

    def _page(self, node: Any, name: str, *, create: bool) -> Any | None:
        pages = [child for child in node.children if not child.is_dir() and child.name == name]
        if len(pages) > 1:
            raise ConflictError(
                f"More than one page named {name!r} in {node.name!r}. "
                "Rename one in LabArchives first."
            )
        if pages:
            return pages[0]
        if create:
            return node.create(labapi.NotebookPage, name)
        return None

    def _find(
        self, page: Any, record_id: str, preview_id: str | None = None
    ) -> tuple[Any | None, Any | None]:
        """The record's attachment entry and preview entry on ``page``, if there.

        The preview is the text entry ``preview_id`` names when it is on the
        page, and otherwise the one that starts with the record's marker.
        """
        caption, marker = RECORD_CAPTION + record_id, _marker(record_id)
        attachments, previews, known = [], [], None
        for entry in page.entries:
            if isinstance(entry, labapi.AttachmentEntry) and entry.caption == caption:
                attachments.append(entry)
            elif isinstance(entry, labapi.TextEntry):
                if preview_id is not None and entry.id == preview_id:
                    known = entry
                elif entry.content.startswith(marker):
                    previews.append(entry)
        if known is not None:
            previews = [known]
        if len(attachments) > 1 or len(previews) > 1:
            raise ConflictError(
                f"The {RECORDS_PAGE} page has more than one entry for record {record_id!r}. "
                "Remove the extra entries in LabArchives first."
            )
        return (attachments[0] if attachments else None, previews[0] if previews else None)

    def _checksum_of(self, data: bytes) -> str:
        """``record_checksum`` of an attachment, or a value that matches nothing."""
        try:
            return record_checksum(Record.model_validate_json(data))
        except ValidationError:
            return f"unreadable:{_sha256(data)}"

    def _read(self, path: LogicalPath, notebook: Any, page: Any, record_id: str) -> StoredRecord:
        attachment_entry, preview_entry = self._find(page, record_id)
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
            container=Container(path=path),
            record_id=record.record_id,
            schema_id=record.schema_id,
            schema_version=record.schema_version,
            checksum=record_checksum(record),
            conflict_token=_sha256(data),
            preview_checksum=preview_checksum(preview_entry.content) if preview_entry else None,
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
