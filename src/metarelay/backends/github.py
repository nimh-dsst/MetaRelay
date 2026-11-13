"""The ``github://`` backend: records as files in a GitHub repository.

Install with ``pip install 'metarelay[github]'``.

Destination
    ``github://<owner>/<repo>@<branch>[/<prefix>]``, for example
    ``github://my-lab/metadata@main/records``. The branch must already
    exist. A branch with ``/`` in its name is written with ``%2F``
    (``@feature%2Fdemo``). Everything MetaRelay writes goes under
    ``prefix`` (or the repository root without one).

Logging in
    A token (``AuthFlow.TOKEN``)::

        session = connect("github://my-lab/metadata@main/records",
                          GitHubConfig(token="github_pat_..."))   # or METARELAY_GITHUB_TOKEN

    A fine-grained token needs *Contents: read and write* on the
    repository. The caller supplies it; MetaRelay keeps it only in memory
    for the session and never writes it anywhere. ``api_url`` points at
    GitHub Enterprise Server (``https://<host>/api/v3``) or a fake.

Layout (plan §3.3), the same as the local backend::

    records/                     the prefix
    └── Fiber study/             a container: a folder
        ├── implant-M1.json      the record: the source of truth
        ├── implant-M1.md        readable preview, rendered by GitHub
        ├── trace.csv            a file from write_file (size-capped)
        └── .metarelay.json      which files are records, and each preview's
                                 checksum as MetaRelay last wrote it (D7)

    Git can't store an empty folder, so ``ensure_container`` writes
    ``.metarelay.json`` into a new one.

Conflicts
    The conflict token is the record file's blob ``sha``. Every write sends
    the ``sha`` it read, so GitHub refuses it (``ConflictError``) if the file
    changed in between, and ``create`` mode is enforced: a ``PUT`` without a
    ``sha`` fails if the file exists.

Limits
    - GitHub is for metadata. ``write_file`` refuses files larger than
      ``GitHubConfig.max_file_bytes`` (1 MB by default).
    - Each changed file is its own commit through the Contents API, so a
      record write is up to three commits (record, preview, index).
    - GitHub lists at most 1,000 entries in one folder.
    - Names are refused if they would break a checkout on macOS or Windows
      (see ``check_local_name``), or differ only in case from a name already
      there.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

from metarelay.backends.local import _fold, _nfc, _nfc_path, _sha256, check_local_name
from metarelay.core.auth import AuthFlow, LoginChallenge
from metarelay.core.checksum import preview_checksum, record_checksum
from metarelay.core.config import DestinationConfig, TokenConfig
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

__all__ = ["GitHubBackend", "GitHubConfig", "GitHubSession"]

BACKEND_NAME = "github"
INDEX_FILE = ".metarelay.json"
INDEX_VERSION = 1
DEFAULT_MAX_FILE_BYTES = 1024 * 1024
HARD_MAX_FILE_BYTES = 50 * 1024 * 1024
"""GitHub warns about files over 50 MB and rejects them over 100 MB."""
_INDEX_RETRIES = 5

_OWNER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class GitHubConfig(TokenConfig):
    """A GitHub token, plus optional settings.

    Reads ``METARELAY_GITHUB_TOKEN``, ``METARELAY_GITHUB_API_URL`` and
    ``METARELAY_GITHUB_MAX_FILE_BYTES`` when not passed.
    """

    model_config = SettingsConfigDict(env_prefix="METARELAY_GITHUB_")

    api_url: str = "https://api.github.com"
    """Where requests go. ``https://<host>/api/v3`` for GitHub Enterprise Server."""
    max_file_bytes: int = Field(default=DEFAULT_MAX_FILE_BYTES, gt=0, le=HARD_MAX_FILE_BYTES)
    """The largest file ``write_file`` accepts."""
    timeout: float = Field(default=30.0, gt=0)
    """Seconds to wait for each request."""

    @field_validator("api_url")
    @classmethod
    def _check_api_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("api_url must be an http:// or https:// URL with a host")
        return value.rstrip("/")


def _config(config: DestinationConfig | None) -> GitHubConfig:
    if isinstance(config, GitHubConfig):
        return config
    if config is None:
        try:
            return GitHubConfig()
        except ConfigurationError:
            if os.environ.get("METARELAY_GITHUB_TOKEN"):
                raise  # the token is there; the error names the setting that isn't valid
            raise ConfigurationError(
                "github:// needs a token. Pass GitHubConfig(token=...) or set "
                "METARELAY_GITHUB_TOKEN."
            ) from None
    if type(config) is TokenConfig:
        return GitHubConfig(token=config.token)
    raise ConfigurationError(f"github:// needs a GitHubConfig, not {type(config).__name__}.")


# Destinations


@dataclass(frozen=True)
class _Address:
    owner: str
    repo: str
    branch: str
    prefix: tuple[str, ...]

    @property
    def uri(self) -> str:
        """The canonical spelling: owner and repo lowercased (GitHub ignores
        their case), branch and prefix percent-encoded."""
        uri = f"github://{self.owner}/{self.repo.lower()}@{quote(self.branch, safe='')}"
        return uri + "".join(f"/{quote(part, safe='')}" for part in self.prefix)


_FORM = "Use github://owner/repo@branch or github://owner/repo@branch/prefix."


def _parse(destination: Destination) -> _Address:
    if "?" in destination.uri or "#" in destination.uri:
        raise ConfigurationError(f"{destination.uri} cannot have a query or fragment. {_FORM}")
    owner = destination.netloc
    if not _OWNER.fullmatch(owner):
        raise ConfigurationError(f"{destination.uri} does not name a GitHub owner. {_FORM}")
    head, _, prefix_text = destination.path.lstrip("/").partition("/")
    repo, at, branch = head.partition("@")
    repo, branch = unquote(repo), unquote(branch)
    if not _REPO.fullmatch(repo) or repo in {".", ".."}:
        raise ConfigurationError(f"{destination.uri} does not name a repository. {_FORM}")
    if not at or not branch:
        raise ConfigurationError(
            f"{destination.uri} does not name a branch. {_FORM} "
            "Write a branch with '/' in its name with %2F."
        )
    if any(unicodedata.category(char) == "Cc" or char.isspace() for char in branch):
        raise ConfigurationError(f"Branch {branch!r} is not a valid branch name.")
    try:
        prefix = LogicalPath(tuple(unquote(part) for part in prefix_text.split("/") if part))
    except ValidationError as error:
        raise ConfigurationError(f"The prefix in {destination.uri} is not valid: {error}") from None
    parts = tuple(_nfc(part) for part in prefix.parts)
    return _Address(owner=owner, repo=repo, branch=branch, prefix=parts)


# Errors


def _github_message(response: httpx.Response) -> str:
    """GitHub's own error text. Never holds the token: it is sent in a header."""
    try:
        message = response.json().get("message", "")
    except (ValueError, AttributeError):
        return ""
    return " ".join(str(message).split())[:300]


def _raise_for(response: httpx.Response, action: str) -> None:
    status = response.status_code
    if status < 400:
        return
    message = _github_message(response)
    detail = f": {message}" if message else "."
    if status == 401:
        raise AuthenticationError(
            f"GitHub rejected the token while trying to {action}. "
            "Check that it is valid and has not expired."
        )
    if status in {403, 429}:
        if (
            status == 429
            or response.headers.get("x-ratelimit-remaining") == "0"
            or "rate limit" in message.lower()
        ):
            raise BackendUnavailableError(
                f"GitHub's rate limit was reached while trying to {action}. Try again later."
            )
        raise PermissionDeniedError(
            f"GitHub refused to {action}{detail} The token needs Contents: read and "
            "write access to this repository."
        )
    if status == 404:
        raise NotFoundError(f"GitHub could not find what it needed to {action}.")
    if status == 409 and "does not match" not in message.lower():
        if any(word in message.lower() for word in ("pull request", "protected", "rule")):
            raise PermissionDeniedError(
                f"GitHub refused to {action}{detail} The branch is protected; use a branch "
                "that takes direct commits."
            )
        raise BackendUnavailableError(f"GitHub refused to {action}{detail}")
    if status == 409:
        raise ConflictError(
            f"Could not {action}: it changed at GitHub since MetaRelay read it. "
            "Read it again, then write."
        )
    if status == 422 and "sha" in message.lower():
        raise ConflictError(f"Could not {action}: it already exists at GitHub.")
    if status == 422:
        raise ValidationError(f"GitHub refused to {action}{detail}")
    if status >= 500:
        raise BackendUnavailableError(
            f"GitHub had a server error ({status}) while trying to {action}."
        )
    raise BackendUnavailableError(f"GitHub answered {status} while trying to {action}{detail}")


@contextlib.contextmanager
def _network_errors(action: str) -> Iterator[None]:
    """Translate ``httpx`` failures. Never chained: they quote the request URL."""
    try:
        yield
    except MetaRelayError:
        raise
    except httpx.HTTPError as error:
        raise BackendUnavailableError(
            f"Could not reach GitHub to {action} ({type(error).__name__})."
        ) from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise BackendUnavailableError(
            f"GitHub sent a response MetaRelay did not understand while trying to {action}."
        ) from None


# Helpers


def _git_sha(data: bytes) -> str:
    """Git's blob ID for ``data``, the ``sha`` GitHub reports for the file."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} bytes"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


def _name_ok(name: str) -> bool:
    try:
        check_local_name(name)
    except ValidationError:
        return False
    return True


def _twin(entries: Mapping[str, _Entry], name: str) -> str | None:
    """A name here equal to ``name`` once folded, but spelled differently."""
    for stored in entries:
        if stored != name and _fold(stored) == _fold(name):
            return stored
    return None


@dataclass(frozen=True)
class _Entry:
    kind: str  # "file" or "dir"
    sha: str


@dataclass
class _Index:
    """``.metarelay.json``: which files are records, and written preview checksums."""

    records: dict[str, dict[str, str]]
    files: list[str]
    sha: str | None

    def to_bytes(self) -> bytes:
        content = {
            "index_version": INDEX_VERSION,
            "records": {key: self.records[key] for key in sorted(self.records)},
            "files": sorted(set(self.files)),
        }
        return (json.dumps(content, indent=2, ensure_ascii=False) + "\n").encode()


def _saved_but(
    error: MetaRelayError, record_id: str, step: str, receipt: Receipt
) -> MetaRelayError:
    """``error``, raised after the record itself was saved.

    Same class, so callers' handling still applies, with a message that says
    the record was saved, and the new receipt as ``error.receipt``.
    """
    new = type(error)(
        f"The record {record_id!r} was saved, but updating {step} failed: {error} "
        "Write the record again, with error.receipt as prior_receipt, to finish."
    )
    new.receipt = receipt  # type: ignore[attr-defined]
    return new


class GitHubSession:
    """A session on a GitHub repository. Create it with ``connect("github://...")``."""

    def __init__(self, http: httpx.Client, address: _Address, max_file_bytes: int) -> None:
        self._http = http
        self._address = address
        self._max_file_bytes = max_file_bytes
        self._repo_url = f"/repos/{quote(address.owner)}/{quote(address.repo)}"
        self._blobs: dict[str, bytes] = {}
        self._closed = False

    @property
    def destination(self) -> str:
        return self._address.uri

    # Containers

    def list_containers(self, parent: LogicalPath = ROOT) -> list[Container]:
        parent = _nfc_path(parent)
        entries = self._folder(parent)
        names = sorted(
            name for name, entry in entries.items() if entry.kind == "dir" and _name_ok(name)
        )
        return [Container(path=parent / name) for name in names]

    def ensure_container(self, path: LogicalPath, *, holds_records: bool = False) -> Container:
        # Every container is a folder here, so holds_records changes nothing.
        path = _nfc_path(path)
        self._check_names(path)
        for _ in range(2):  # a second pass if someone created it at the same moment
            entries = self._listing(ROOT) or {}
            for depth, part in enumerate(path.parts):
                here = LogicalPath(path.parts[: depth + 1])
                entry = entries.get(part)
                if entry is None:
                    self._refuse_twin(entries, part, here)
                    break
                if entry.kind != "dir":
                    raise ValidationError(f"{self._describe(here)} is a file, not a folder.")
                if depth < len(path.parts) - 1:
                    entries = self._listing(here) or {}
            else:
                return Container(path=path)
            # Writing the index creates the folder and every missing parent.
            try:
                self._put(
                    self._git_path(path, INDEX_FILE),
                    _Index(records={}, files=[], sha=None).to_bytes(),
                    sha=None,
                    message=f"MetaRelay: create {self._describe(path)}",
                    action=f"create {self._describe(path)}",
                )
            except ConflictError:
                continue
            return Container(path=path)
        raise ConflictError(f"{self._describe(path)} kept changing while MetaRelay created it.")

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
        record_id = check_local_name(record.record_id, "Record ID")
        json_name, md_name = f"{record_id}.json", f"{record_id}.md"
        if not _name_ok(md_name) or not _name_ok(json_name):
            raise ValidationError(f"Record ID {record_id!r} is too long to use as a file name.")
        entries = self._folder(path)
        index = self._index(path, entries)
        self._refuse_name_clash(entries, index, record_id)
        json_entry, md_entry = entries.get(json_name), entries.get(md_name)
        for name, entry in ((json_name, json_entry), (md_name, md_entry)):
            if entry is not None and entry.kind != "file":
                raise ValidationError(f"{name!r} is a folder at the destination.")
        where = f"write {record_id!r} in {self._describe(path)}"

        remote_bytes = self._blob(json_entry.sha, where) if json_entry else None
        remote_preview = self._blob(md_entry.sha, where) if md_entry else None
        remote_preview_sum = (
            preview_checksum(remote_preview.decode(errors="replace"))
            if remote_preview is not None
            else None
        )
        action = decide_write(  # also rejects an unknown mode
            mode=mode,
            local_checksum=record_checksum(record),
            remote_checksum=self._checksum_of(remote_bytes) if remote_bytes is not None else None,
            prior_receipt=prior_receipt,
            remote_preview_checksum=remote_preview_sum,
        )

        def receipt(token: str, preview_sum: str | None) -> Receipt:
            return Receipt(
                destination=self.destination,
                backend=BACKEND_NAME,
                container=Container(path=path),
                record_id=record.record_id,
                schema_id=record.schema_id,
                schema_version=record.schema_version,
                checksum=record_checksum(record),
                conflict_token=token,
                preview_checksum=preview_sum,
                backend_ids={"path": self._git_path(path, json_name)},
            )

        preview = render_preview(record).markdown
        new_preview_sum = preview_checksum(preview)
        written_preview = self._written_preview(index, record_id)
        if action is WriteAction.WRITE:
            data = (record.model_dump_json(indent=2) + "\n").encode()
            verb = "create" if json_entry is None else "update"
            token = self._put_if_changed(
                self._git_path(path, json_name),
                data,
                json_entry,
                message=f"MetaRelay: {verb} record {record_id} in {self._describe(path)}",
                action=where,
            )
            # The record is saved. Each step below is another commit, so a
            # failure from here on must say so and hand back the receipt.
            try:
                self._put_if_changed(
                    self._git_path(path, md_name),
                    preview.encode(),
                    md_entry,
                    message=f"MetaRelay: preview of {record_id} in {self._describe(path)}",
                    action=f"write the preview of {record_id!r}",
                )
            except ConflictError:
                error = ConflictError(
                    f"The record {record_id!r} was saved, but its preview was edited at GitHub "
                    "while MetaRelay was writing it. Read the record, then write again with "
                    "mode='force' to replace the edited preview."
                )
                error.receipt = receipt(token, written_preview)  # type: ignore[attr-defined]
                raise error from None
            except MetaRelayError as error:
                raise _saved_but(
                    error, record_id, "its preview", receipt(token, written_preview)
                ) from None
            written_preview = new_preview_sum
        else:
            assert json_entry is not None  # SKIP means the record is there
            token = json_entry.sha
            if (
                prior_receipt is not None
                and remote_preview_sum is not None
                and remote_preview_sum == prior_receipt.preview_checksum
            ):
                # The caller's receipt says MetaRelay wrote this preview, even
                # if the index missed it (an earlier write failed partway).
                written_preview = remote_preview_sum
            # Same content: leave the record alone. Restore a missing preview
            # and refresh a stale one MetaRelay wrote; an edited one is kept.
            stale = remote_preview_sum is not None and remote_preview_sum == written_preview
            if md_entry is None or (stale and remote_preview_sum != new_preview_sum):
                self._put(
                    self._git_path(path, md_name),
                    preview.encode(),
                    sha=md_entry.sha if md_entry else None,
                    message=f"MetaRelay: restore preview of {record_id}",
                    action=f"restore the preview of {record_id!r}",
                )
                written_preview = new_preview_sum
        if written_preview is not None:
            remembered = {"preview_checksum": written_preview}
            if index.sha is None or index.records.get(record_id) != remembered:
                try:
                    self._update_index(
                        path,
                        lambda latest: latest.records.update({record_id: remembered}),
                        current=index,
                    )
                except MetaRelayError as error:
                    raise _saved_but(
                        error,
                        record_id,
                        f"the index ({INDEX_FILE})",
                        receipt(token, written_preview),
                    ) from None
        else:
            written_preview = remote_preview_sum

        return receipt(token, written_preview)

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        path = _nfc_path(path)
        if not _name_ok(record_id) or not _name_ok(f"{record_id}.json"):
            # Still report a missing container as such.
            self._folder(path)
            raise NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        entries = self._folder(path)
        index = self._index(path, entries)
        if _fold(f"{record_id}.json") in {_fold(name) for name in index.files}:
            raise NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        return self._read(path, entries, index, record_id)

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        path = _nfc_path(path)
        entries = self._folder(path)
        index = self._index(path, entries)
        found = []
        for record_id in self._record_ids(entries, index):
            try:
                found.append(self._read(path, entries, index, record_id))
            except NotFoundError:
                continue  # in the index, but deleted by hand
            except ValidationError:
                # In a folder MetaRelay manages, a .json it never wrote that
                # isn't a record (package.json at a repository root) is someone
                # else's file. A damaged record MetaRelay wrote still fails loudly.
                if index.sha is not None and record_id not in index.records:
                    continue
                raise
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
        name = _nfc(check_local_name(name, "File name"))
        data = bytes(data)
        if len(data) > self._max_file_bytes:
            raise ValidationError(
                f"{name!r} is {_size(len(data))}, and this GitHub destination takes files up "
                f"to {_size(self._max_file_bytes)} (GitHubConfig.max_file_bytes). GitHub is "
                "for metadata: keep large files in your data store and record where they are."
            )
        entries = self._folder(path)
        index = self._index(path, entries)
        entry = entries.get(name)
        if entry is not None and entry.kind != "file":
            raise ValidationError(f"{name!r} is a folder at the destination.")
        twin = _twin(entries, name)
        if twin is not None:
            raise ValidationError(
                f"{name!r} differs only in case from {twin!r}, which is already here. "
                "Use the same spelling, or another name."
            )
        owned = {
            _fold(f"{record_id}{suffix}")
            for record_id in self._record_ids(entries, index)
            for suffix in (".json", ".md")
        }
        if _fold(name) in owned:
            raise ValidationError(
                f"{name!r} is a record's file name. Use another name for this file."
            )

        # Index first, so the upload is never mistaken for a record. If the
        # upload fails, take a name it added back out: the files list decides
        # what counts as a record, so a stray name would hide one.
        newly_listed = name not in index.files
        self._update_index(path, lambda latest: latest.files.append(name), current=index)
        try:
            sha = self._put_if_changed(
                self._git_path(path, name),
                data,
                entry,
                message=f"MetaRelay: write {name} in {self._describe(path)}",
                action=f"write {name!r} in {self._describe(path)}",
                cache=False,
            )
        except MetaRelayError:
            if newly_listed and entry is None:
                with contextlib.suppress(MetaRelayError):
                    self._update_index(
                        path,
                        lambda latest: latest.files.remove(name) if name in latest.files else None,
                    )
            raise
        return FileReceipt(
            destination=self.destination,
            backend=BACKEND_NAME,
            container=Container(path=path),
            name=name,
            checksum=_sha256(data),
            size=len(data),
            native_id=sha,
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._http.close()

    # HTTP

    def _request(
        self,
        method: str,
        url: str,
        action: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        """Send a request; return its JSON, or ``None`` for a 404 if ``missing_ok``."""
        if self._closed:
            raise ConfigurationError("This GitHub session is closed. Connect again.")
        with _network_errors(action):
            response = self._http.request(method, url, params=params, json=body)
            if response.status_code == 404 and "no commit found for the ref" in (
                _github_message(response).lower()
            ):
                raise NotFoundError(self._branch_gone())
            if missing_ok and response.status_code == 404:
                return None
            _raise_for(response, action)
            return response.json()

    def _git_path(self, path: LogicalPath, *names: str) -> str:
        return "/".join((*self._address.prefix, *path.parts, *names))

    def _contents_url(self, git_path: str) -> str:
        if not git_path:
            return f"{self._repo_url}/contents"
        return f"{self._repo_url}/contents/{quote(git_path)}"

    def _listing(self, path: LogicalPath) -> dict[str, _Entry] | None:
        """The folder at ``path``, ``name -> entry``, or ``None`` if it doesn't exist."""
        action = f"list {self._describe(path)}"
        found = self._request(
            "GET",
            self._contents_url(self._git_path(path)),
            action,
            params={"ref": self._address.branch},
            missing_ok=True,
        )
        if found is None:
            return None
        if not isinstance(found, list):
            raise ValidationError(f"{self._describe(path)} is a file, not a folder.")
        with _network_errors(action):
            return {
                _nfc(item["name"]): _Entry(
                    kind="dir" if item["type"] == "dir" else "file",
                    sha=item["sha"],
                )
                for item in found
            }

    def _folder(self, path: LogicalPath) -> dict[str, _Entry]:
        """The folder at ``path``. Raises ``NotFoundError`` if it doesn't exist.

        The destination root always exists, even before anything is written
        under the prefix.
        """
        self._check_names(path)
        entries = self._listing(path)
        if entries is not None:
            return entries
        if not path.parts:
            # The prefix isn't created yet, or the repository is gone. Tell
            # them apart, so lost access never reads as an empty destination.
            branch_url = f"{self._repo_url}/branches/{quote(self._address.branch)}"
            if self._request("GET", branch_url, "open the branch", missing_ok=True) is None:
                raise NotFoundError(self._branch_gone())
            return {}
        # Explain why: a name that differs only in case is the usual reason.
        parent = self._listing(path.parent)
        if parent is not None:
            self._refuse_twin(parent, path.name, path)
        raise NotFoundError(
            f"{self._describe(path)} does not exist. Create it first "
            "(ensure_container(), or metarelay mkdir)."
        )

    def _branch_gone(self) -> str:
        return (
            f"{self._address.owner}/{self._address.repo} has no branch "
            f"{self._address.branch!r} any more, or the token can no longer see it."
        )

    def _blob(self, sha: str, action: str) -> bytes:
        """A file's bytes by blob ``sha``. Works for any size, and never changes,
        so it is cached for the session."""
        cached = self._blobs.get(sha)
        if cached is not None:
            return cached
        found = self._request("GET", f"{self._repo_url}/git/blobs/{sha}", action)
        with _network_errors(action):
            if found.get("encoding") != "base64":
                raise ValueError("unexpected blob encoding")
            data = base64.b64decode(found["content"])
        if _git_sha(data) != sha:
            raise BackendUnavailableError(
                f"GitHub sent damaged content while MetaRelay tried to {action}."
            )
        if len(self._blobs) > 1024:
            self._blobs.clear()
        self._blobs[sha] = data
        return data

    def _put(
        self,
        git_path: str,
        data: bytes,
        *,
        sha: str | None,
        message: str,
        action: str,
        cache: bool = True,
    ) -> str:
        """Create or replace a file in one commit. Returns its new blob ``sha``."""
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(data).decode(),
            "branch": self._address.branch,
        }
        if sha is not None:
            body["sha"] = sha
        result = self._request("PUT", self._contents_url(git_path), action, body=body)
        with _network_errors(action):
            new_sha = result["content"]["sha"]
            if not isinstance(new_sha, str):
                raise TypeError
        if cache:  # records, previews and indexes, which are read back; not uploads
            self._blobs[new_sha] = data
        return new_sha

    def _put_if_changed(
        self,
        git_path: str,
        data: bytes,
        entry: _Entry | None,
        *,
        message: str,
        action: str,
        cache: bool = True,
    ) -> str:
        """``_put``, skipped (no empty commit) when the file already holds ``data``."""
        if entry is not None and entry.sha == _git_sha(data):
            return entry.sha
        return self._put(
            git_path,
            data,
            sha=entry.sha if entry else None,
            message=message,
            action=action,
            cache=cache,
        )

    # The index

    def _index(self, path: LogicalPath, entries: Mapping[str, _Entry]) -> _Index:
        entry = entries.get(INDEX_FILE)
        if entry is None or entry.kind != "file":
            return _Index(records={}, files=[], sha=None)
        data = self._blob(entry.sha, f"read the index of {self._describe(path)}")
        try:
            content = json.loads(data)
            records, files = content["records"], content["files"]
            if not isinstance(records, dict) or not isinstance(files, list):
                raise TypeError
            if not all(isinstance(name, str) for name in files):
                raise TypeError
            if not all(isinstance(value, dict) for value in records.values()):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise ValidationError(
                f"{self._git_path(path, INDEX_FILE)} is damaged. Delete it in the repository; "
                "MetaRelay then treats every .json file there as a record."
            ) from None
        return _Index(records=records, files=files, sha=entry.sha)

    def _update_index(
        self,
        path: LogicalPath,
        change: Callable[[_Index], None],
        *,
        current: _Index | None = None,
    ) -> None:
        """Apply ``change`` to the index, retrying if another writer got there first.

        ``current`` is an index the caller just read; the first attempt uses it
        instead of reading again. A stale one only costs a retry.
        """
        for attempt in range(_INDEX_RETRIES):
            if attempt == 0 and current is not None:
                index = _Index(
                    records=dict(current.records), files=list(current.files), sha=current.sha
                )
            else:
                index = self._index(path, self._folder(path))
            before = index.to_bytes()
            change(index)
            after = index.to_bytes()
            if index.sha is not None and after == before:
                return
            try:
                self._put(
                    self._git_path(path, INDEX_FILE),
                    after,
                    sha=index.sha,
                    message=f"MetaRelay: update the index of {self._describe(path)}",
                    action=f"update the index of {self._describe(path)}",
                )
            except ConflictError:
                continue
            return
        raise ConflictError(
            f"The index of {self._describe(path)} kept changing while MetaRelay updated it. "
            "Try again."
        )

    def _written_preview(self, index: _Index, record_id: str) -> str | None:
        checksum = index.records.get(record_id, {}).get("preview_checksum")
        if checksum is None:
            return None
        if not isinstance(checksum, str) or not _HEX64.fullmatch(checksum):
            raise ValidationError(
                f"The index entry for {record_id!r} is damaged. Delete it from "
                f"{INDEX_FILE}; MetaRelay then compares edits against the preview as it is now."
            )
        return checksum

    def _record_ids(self, entries: Mapping[str, _Entry], index: _Index) -> list[str]:
        """Records here: indexed ones plus every ``{id}.json`` that isn't a file.

        Scanned too, so a record whose index entry was lost still shows up. Not
        parsed: a damaged record still counts, so reading it fails loudly.
        """
        files = {_fold(name) for name in index.files}
        ids = set(index.records)
        for name, entry in entries.items():
            record_id = name.removesuffix(".json")
            if entry.kind == "file" and name.endswith(".json") and _name_ok(record_id):
                ids.add(record_id)
        return sorted(rid for rid in ids if _fold(f"{rid}.json") not in files)

    # Checks

    def _describe(self, path: LogicalPath) -> str:
        return f"container {str(path)!r}" if path.parts else "the destination root"

    def _check_names(self, path: LogicalPath) -> None:
        for part in path.parts:
            check_local_name(part, "Container name")

    def _refuse_twin(self, entries: Mapping[str, _Entry], name: str, here: LogicalPath) -> None:
        twin = _twin(entries, name)
        if twin is not None:
            raise ValidationError(
                f"{self._describe(here)} differs only in case from {twin!r}, which already "
                "exists. Use the existing spelling."
            )

    def _refuse_name_clash(
        self, entries: Mapping[str, _Entry], index: _Index, record_id: str
    ) -> None:
        files = {_fold(name) for name in index.files}
        for name in (f"{record_id}.json", f"{record_id}.md"):
            if _fold(name) in files:
                raise ValidationError(
                    f"{name!r} is a file written with write_file, not a record. "
                    "Use another record ID."
                )
            twin = _twin(entries, name)
            if twin is not None:
                raise ValidationError(
                    f"Record ID {record_id!r} differs only in case from {twin!r}, which is "
                    "already here. IDs that differ only in case can't share a container."
                )

    def _checksum_of(self, data: bytes) -> str:
        """``record_checksum`` of stored bytes, or a value that matches nothing."""
        try:
            return record_checksum(Record.model_validate_json(data))
        except ValidationError:
            return "unreadable:" + _sha256(data)

    def _read(
        self, path: LogicalPath, entries: Mapping[str, _Entry], index: _Index, record_id: str
    ) -> StoredRecord:
        missing = NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        json_entry = entries.get(f"{record_id}.json")
        if json_entry is None or json_entry.kind != "file":
            raise missing
        where = f"read {record_id!r} in {self._describe(path)}"
        data = self._blob(json_entry.sha, where)
        git_path = self._git_path(path, f"{record_id}.json")
        try:
            record = Record.model_validate_json(data)
        except ValidationError as error:
            raise ValidationError(f"{git_path} is not a valid record: {error}") from error
        if record.record_id != record_id:
            raise ValidationError(
                f"{git_path} holds record {record.record_id!r}, not {record_id!r}. "
                "Rename the file or fix its record_id."
            )
        # The preview as MetaRelay last wrote it, so a hand edit still
        # conflicts after a read. Records MetaRelay didn't index fall back to
        # the preview as it is now.
        written = self._written_preview(index, record_id)
        if written is None:
            md_entry = entries.get(f"{record_id}.md")
            if md_entry is not None and md_entry.kind == "file":
                preview = self._blob(md_entry.sha, where)
                written = preview_checksum(preview.decode(errors="replace"))
        receipt = Receipt(
            destination=self.destination,
            backend=BACKEND_NAME,
            container=Container(path=path),
            record_id=record.record_id,
            schema_id=record.schema_id,
            schema_version=record.schema_version,
            checksum=record_checksum(record),
            conflict_token=json_entry.sha,
            preview_checksum=written,
            backend_ids={"path": git_path},
        )
        return StoredRecord(record=record, receipt=receipt)


class GitHubBackend:
    """The ``github://`` backend. Registered under ``metarelay.backends``."""

    name = BACKEND_NAME
    auth_flow = AuthFlow.TOKEN

    def open(self, destination: Destination, config: DestinationConfig | None) -> GitHubSession:
        address = _parse(destination)
        settings = _config(config)
        http = httpx.Client(
            base_url=settings.api_url,
            headers={
                "Authorization": f"Bearer {settings.token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "metarelay",
            },
            timeout=settings.timeout,
            follow_redirects=True,  # renamed repositories; httpx drops the token across hosts
        )
        session = GitHubSession(http, address, settings.max_file_bytes)
        try:
            self._check(session, address)
        except BaseException:
            session.close()
            raise
        return session

    def _check(self, session: GitHubSession, address: _Address) -> None:
        full_name = f"{address.owner}/{address.repo}"
        repo = session._request("GET", session._repo_url, "open the repository", missing_ok=True)
        if repo is None:
            raise ConfigurationError(
                f"GitHub has no repository {full_name}, or the token can't see it."
            )
        branch = session._request(
            "GET",
            f"{session._repo_url}/branches/{quote(address.branch)}",
            "open the branch",
            missing_ok=True,
        )
        if branch is None:
            raise ConfigurationError(
                f"{full_name} has no branch {address.branch!r}. Create it first (an empty "
                "repository needs a first commit)."
            )

    def begin_login(
        self, destination: Destination, redirect_url: str, config: DestinationConfig | None
    ) -> LoginChallenge:
        raise ConfigurationError("github:// uses a token, not a browser login. Use connect().")

    def complete_login(
        self,
        challenge: LoginChallenge,
        params: Mapping[str, str],
        config: DestinationConfig | None,
    ) -> GitHubSession:
        raise ConfigurationError("github:// uses a token, not a browser login. Use connect().")
