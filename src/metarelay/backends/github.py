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
    The conflict token is the record file's blob ``sha``. A write reads the
    branch head, asks ``decide_write`` about the record and preview as they
    are in that commit, and commits the record, preview, and index together
    on top of it. The branch is moved only if it still points at that head
    (a fast forward), so nothing lands on a change MetaRelay didn't see.

Limits
    - GitHub is for metadata. ``write_file`` refuses files larger than
      ``GitHubConfig.max_file_bytes`` (1 MB by default).
    - Each write is one commit, through the Git Data API. A write that
      changes nothing makes none.
    - Names are refused if they would break a checkout on macOS or Windows
      (see ``check_local_name``), or differ only in case from a name already
      there. The prefix in the URI is checked the same way.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import random
import re
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar
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
from metarelay.core.preview import PreviewDiff, preview_diff, render_preview
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
_BRANCH_RETRIES = 6
_BRANCH_BACKOFF = 0.25
"""Seconds before the first retry when other commits moved the branch; doubles each time."""
_REPLICA_LAG = 10.0
"""Seconds after its own commit that a session distrusts a branch read
reporting an older commit (GitHub replica lag). Later, it is a real reset."""
_CACHE_LIMIT = 1024
"""Git objects kept per session, per kind. They never change, so any is safe to keep."""

_OWNER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_T = TypeVar("_T")


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
    for part in parts:
        try:
            # A leading '.' is reserved inside containers, not in your prefix.
            check_local_name(f"_{part}" if part.startswith(".") else part, "Prefix folder")
        except ValidationError as error:
            raise ConfigurationError(
                f"The prefix in {destination.uri} is not valid: {error}"
            ) from None
    return _Address(owner=owner, repo=repo, branch=branch, prefix=parts)


# Errors


def _github_message(response: httpx.Response) -> str:
    """GitHub's own error text. Never holds the token: it is sent in a header."""
    try:
        message = response.json().get("message", "")
    except (ValueError, AttributeError):
        return ""
    return " ".join(str(message).split())[:300]


class _BranchMoved(BackendUnavailableError):
    """Another commit landed on the branch during a write. Safe to resend."""


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
    lower = message.lower()
    if status == 404:
        raise NotFoundError(f"GitHub could not find what it needed to {action}.")
    if status == 422 and any(
        words in lower
        for words in ("fast forward", "reference cannot be updated", "reference update failed")
    ):
        # Another commit (to any file) landed on the branch after MetaRelay
        # read it. Not a conflict on ours: read again and retry.
        raise _BranchMoved(
            f"Other commits kept landing on the branch while MetaRelay tried to {action}. "
            "Try again."
        )
    if status in {409, 422} and any(w in lower for w in ("pull request", "protected", "rule")):
        raise PermissionDeniedError(
            f"GitHub refused to {action}{detail} The branch is protected; use a branch "
            "that takes direct commits."
        )
    if status == 409 and "empty" in lower:
        raise NotFoundError(f"The repository is empty, so MetaRelay could not {action}.")
    if status == 409:
        raise BackendUnavailableError(f"GitHub refused to {action}{detail}")
    if status == 422 and "reference does not exist" in lower:
        # The branch was deleted after MetaRelay read it.
        raise NotFoundError(f"The branch is gone, so MetaRelay could not {action}.")
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


def _parse_record(data: bytes) -> Record | None:
    try:
        return Record.model_validate_json(data)
    except ValidationError:
        return None


def _remember(cache: dict[str, Any], key: str, value: Any) -> None:
    if len(cache) >= _CACHE_LIMIT:
        cache.clear()
    cache[key] = value


def _pin(
    pinned: dict[str, str | None],
    seen: Mapping[str, str | None],
    action: str,
    ours: Mapping[str, str],
) -> None:
    """Refuse a retry if a file the write touches changed since the first attempt.

    The first attempt records each file's blob ``sha`` (``None`` if absent).
    A retry after the branch moved may commit on top of other people's
    changes, but not on top of a change to these files, unless the change
    is the very bytes this write would commit (``ours``: name -> sha).
    """
    if not pinned:
        pinned.update(seen)
        return
    for name, sha in seen.items():
        if sha == pinned[name] or (sha is not None and sha == ours.get(name)):
            continue
        if pinned[name] is None:
            raise ConflictError(
                f"Could not {action}: {name!r} already exists at GitHub. It was created "
                "while MetaRelay was writing."
            )
        raise ConflictError(
            f"Could not {action}: {name!r} changed at GitHub since MetaRelay read it. "
            "Read it again, then write."
        )


@dataclass(frozen=True)
class _Entry:
    kind: str  # "file", "dir", or "other" (a submodule)
    sha: str
    stored: str = ""
    """The name as committed. Entries are keyed by its NFC form; they differ
    for a name committed in NFD (some Linux tools do this)."""


@dataclass(frozen=True)
class _Snapshot:
    """One commit on the branch. Every read in an operation comes from it."""

    commit: str
    tree: str


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


def _stored_preview_diff(
    stored: bytes | None, preview: bytes | None, written_preview: str | None
) -> PreviewDiff | None:
    """What someone changed in the ``.md`` preview, compared with the stored record.

    MetaRelay commits a record and its preview together, but the preview can
    still lag the record: someone changed the ``.json`` at GitHub and left
    the preview alone. Compared with the stored record, the preview's old
    values would then read as a person's edits, and accepting them would
    undo that change. So the diff is complete only when the preview
    MetaRelay last wrote (the index's checksum) is the stored record's own
    render. Otherwise it lists no edits: MetaRelay can't separate them.
    """
    text = preview.decode(errors="replace") if preview is not None else None
    diff = preview_diff(stored, text, format="markdown")
    if diff is None or not diff.complete:
        return diff
    try:
        rendered = render_preview(Record.model_validate_json(stored or b"")).markdown
    except ValueError:
        rendered = None
    if rendered is None or preview_checksum(rendered) != written_preview:
        # The preview lags the record, so its differences mix a person's
        # edits with values from before the .json changed. List none of them.
        return PreviewDiff(edits=[], complete=False)
    return diff


class GitHubSession:
    """A session on a GitHub repository. Create it with ``connect("github://...")``."""

    def __init__(self, http: httpx.Client, address: _Address, max_file_bytes: int) -> None:
        self._http = http
        self._address = address
        self._max_file_bytes = max_file_bytes
        self._repo_url = f"/repos/{quote(address.owner)}/{quote(address.repo)}"
        self._ref_path = f"heads/{quote(address.branch)}"
        self._blobs: dict[str, bytes] = {}
        self._trees: dict[str, dict[str, _Entry]] = {}
        self._commit_trees: dict[str, str] = {}
        self._latest: str | None = None
        """The last commit this session landed."""
        self._landed_at = 0.0
        self._replaced: dict[str, None] = {}
        """Commits this session's own commits replaced on the branch (an ordered set)."""
        self._closed = False

    @property
    def destination(self) -> str:
        return self._address.uri

    # Containers

    def list_containers(self, parent: LogicalPath = ROOT) -> list[Container]:
        parent = _nfc_path(parent)
        entries = self._folder(self._head(), parent)
        names = sorted(
            name for name, entry in entries.items() if entry.kind == "dir" and _name_ok(name)
        )
        return [Container(path=parent / name) for name in names]

    def ensure_container(self, path: LogicalPath, *, holds_records: bool = False) -> Container:
        # Every container is a folder here, so holds_records changes nothing.
        path = _nfc_path(path)
        self._check_names(path)
        where = f"create {self._describe(path)}"

        def attempt() -> Container:
            snapshot = self._head()
            entries = self._listing(snapshot, ROOT) or {}
            for depth, part in enumerate(path.parts):
                here = LogicalPath(path.parts[: depth + 1])
                entry = entries.get(part)
                if entry is None:
                    self._refuse_twin(entries, part, here)
                    break
                if entry.kind != "dir":
                    raise ValidationError(f"{self._describe(here)} is a file, not a folder.")
                self._refuse_other_form(entry, part)
                if depth < len(path.parts) - 1:
                    entries = self._listing(snapshot, here) or {}
            else:
                return Container(path=path)
            # Git has no empty folders: the index creates the folder and its parents.
            index = _Index(records={}, files=[], sha=None).to_bytes()
            self._commit(
                snapshot,
                {self._git_path(path, INDEX_FILE): index},
                f"MetaRelay: create {self._describe(path)}",
                where,
            )
            return Container(path=path)

        return self._with_retries(where, attempt)

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
        where = f"write {record_id!r} in {self._describe(path)}"
        json_path, md_path = self._git_path(path, json_name), self._git_path(path, md_name)
        index_path = self._git_path(path, INDEX_FILE)
        data = (record.model_dump_json(indent=2) + "\n").encode()
        markdown = render_preview(record).markdown
        preview, new_preview_sum = markdown.encode(), preview_checksum(markdown)
        pinned: dict[str, str | None] = {}

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
                backend_ids={"path": json_path},
            )

        def attempt() -> Receipt:
            snapshot = self._head()
            entries = self._folder(snapshot, path)
            index = self._index(path, entries)
            self._refuse_name_clash(entries, index, record_id)
            json_entry, md_entry = entries.get(json_name), entries.get(md_name)
            for name, entry in ((json_name, json_entry), (md_name, md_entry)):
                if entry is not None and entry.kind != "file":
                    raise ValidationError(f"{name!r} is a folder at the destination.")
            _pin(
                pinned,
                {
                    json_name: json_entry.sha if json_entry else None,
                    md_name: md_entry.sha if md_entry else None,
                },
                where,
                ours={json_name: _git_sha(data), md_name: _git_sha(preview)},
            )

            remote_bytes = self._blob(json_entry.sha, where) if json_entry else None
            remote_preview = self._blob(md_entry.sha, where) if md_entry else None
            remote_preview_sum = (
                preview_checksum(remote_preview.decode(errors="replace"))
                if remote_preview is not None
                else None
            )
            written_preview = self._written_preview(index, record_id)
            action = decide_write(  # also rejects an unknown mode
                mode=mode,
                local_checksum=record_checksum(record),
                remote_checksum=self._checksum_of(remote_bytes)
                if remote_bytes is not None
                else None,
                prior_receipt=prior_receipt,
                remote_preview_checksum=remote_preview_sum,
                preview_diff=lambda: _stored_preview_diff(
                    remote_bytes, remote_preview, written_preview
                ),
            )

            changes: dict[str, bytes] = {}
            if action is WriteAction.WRITE:
                verb = "create" if json_entry is None else "update"
                message = f"MetaRelay: {verb} record {record_id} in {self._describe(path)}"
                token = _git_sha(data)
                if json_entry is None or json_entry.sha != token:
                    changes[json_path] = data
                if md_entry is None or md_entry.sha != _git_sha(preview):
                    changes[md_path] = preview
                written_preview = new_preview_sum
            else:
                # SKIP: the same record is there, so json_entry exists.
                assert json_entry is not None
                message = f"MetaRelay: restore the preview of {record_id}"
                token = json_entry.sha
                # Leave the record alone. Restore a missing preview and refresh
                # a stale one MetaRelay wrote; an edited one is kept.
                stale = remote_preview_sum is not None and remote_preview_sum == written_preview
                if md_entry is None or (stale and remote_preview_sum != new_preview_sum):
                    changes[md_path] = preview
                    written_preview = new_preview_sum
            if written_preview is not None:
                remembered = {"preview_checksum": written_preview}
                if index.sha is None or index.records.get(record_id) != remembered:
                    index.records[record_id] = remembered
                    changes[index_path] = index.to_bytes()
                    if len(changes) == 1:
                        message = f"MetaRelay: update the index of {self._describe(path)}"
            else:
                written_preview = remote_preview_sum

            if changes:
                self._commit(snapshot, changes, message, where)
                for content in changes.values():
                    _remember(self._blobs, _git_sha(content), content)
            return receipt(token, written_preview)

        return self._with_retries(where, attempt)

    def read_record(self, path: LogicalPath, record_id: str) -> StoredRecord:
        path = _nfc_path(path)
        snapshot = self._head()
        if not _name_ok(record_id) or not _name_ok(f"{record_id}.json"):
            # Still report a missing container as such.
            self._folder(snapshot, path)
            raise NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        entries = self._folder(snapshot, path)
        index = self._index(path, entries)
        if _fold(f"{record_id}.json") in {_fold(name) for name in index.files}:
            raise NotFoundError(f"No record {record_id!r} in {self._describe(path)}.")
        return self._read(path, entries, index, record_id)

    def list_records(self, path: LogicalPath) -> list[StoredRecord]:
        path = _nfc_path(path)
        entries = self._folder(self._head(), path)
        index = self._index(path, entries)
        found = []
        for record_id in self._record_ids(entries, index):
            try:
                found.append(self._read(path, entries, index, record_id))
            except NotFoundError:
                continue  # in the index, but deleted by hand
            except ValidationError:
                # A .json MetaRelay never indexed that isn't a record
                # (package.json at a repository root) is someone else's file.
                # A damaged record MetaRelay wrote still fails loudly, and
                # read_record of it too.
                if record_id not in index.records:
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
        where = f"write {name!r} in {self._describe(path)}"
        sha = _git_sha(data)
        pinned: dict[str, str | None] = {}

        def attempt() -> FileReceipt:
            snapshot = self._head()
            entries = self._folder(snapshot, path)
            index = self._index(path, entries)
            entry = entries.get(name)
            if entry is not None and entry.kind != "file":
                raise ValidationError(f"{name!r} is a folder at the destination.")
            self._refuse_other_form(entry, name)
            twin = _twin(entries, name)
            if twin is not None:
                raise ValidationError(
                    f"{name!r} differs only in case from {twin!r}, which is already here. "
                    "Use the same spelling, or another name."
                )
            if self._is_records_file(path, entries, index, name):
                raise ValidationError(
                    f"{name!r} is a record's file name. Use another name for this file."
                )
            _pin(pinned, {name: entry.sha if entry else None}, where, ours={name: sha})

            # The file and its line in the index land in one commit, so an
            # upload is never mistaken for a record.
            changes: dict[str, bytes] = {}
            if entry is None or entry.sha != sha:
                changes[self._git_path(path, name)] = data
            if name not in index.files:
                index.files.append(name)
                index_bytes = index.to_bytes()
                changes[self._git_path(path, INDEX_FILE)] = index_bytes
                _remember(self._blobs, _git_sha(index_bytes), index_bytes)
            if changes:
                self._commit(
                    snapshot, changes, f"MetaRelay: write {name} in {self._describe(path)}", where
                )
            return FileReceipt(
                destination=self.destination,
                backend=BACKEND_NAME,
                container=Container(path=path),
                name=name,
                checksum=_sha256(data),
                size=len(data),
                native_id=sha,
            )

        return self._with_retries(where, attempt)

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
        body: Mapping[str, Any] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        """Send a request; return its JSON, or ``None`` for a 404 if ``missing_ok``."""
        if self._closed:
            raise ConfigurationError("This GitHub session is closed. Connect again.")
        with _network_errors(action):
            response = self._http.request(method, url, json=body)
            if missing_ok and response.status_code == 404:
                return None
            _raise_for(response, action)
            return response.json()

    def _git_path(self, path: LogicalPath, *names: str) -> str:
        return "/".join((*self._address.prefix, *path.parts, *names))

    # Reading one commit

    def _head(self) -> _Snapshot:
        """The commit the branch points at now."""
        action = f"read the branch {self._address.branch!r}"
        ref = self._request(
            "GET", f"{self._repo_url}/git/ref/{self._ref_path}", action, missing_ok=True
        )
        if ref is None:
            raise NotFoundError(self._branch_gone())
        with _network_errors(action):
            commit = ref["object"]["sha"]
            if not isinstance(commit, str):
                raise TypeError
        if (
            commit in self._replaced
            and self._latest is not None
            and time.monotonic() - self._landed_at < _REPLICA_LAG
        ):
            # Right after a commit, GitHub can answer from a replica that
            # hasn't seen it yet. Don't read from before our own write. Only
            # briefly: later, an older commit means someone reset the branch
            # (to undo our write, say), and building on ours would bring it back.
            commit = self._latest
        tree = self._commit_trees.get(commit)
        if tree is None:
            found = self._request("GET", f"{self._repo_url}/git/commits/{commit}", action)
            with _network_errors(action):
                tree = found["tree"]["sha"]
                if not isinstance(tree, str):
                    raise TypeError
            _remember(self._commit_trees, commit, tree)
        return _Snapshot(commit=commit, tree=tree)

    def _tree(self, sha: str, where: str) -> dict[str, _Entry]:
        """One folder of a commit, ``NFC name -> entry``. Trees never change, so cached."""
        cached = self._trees.get(sha)
        if cached is not None:
            return cached
        action = f"list {where}"
        found = self._request("GET", f"{self._repo_url}/git/trees/{sha}", action)
        with _network_errors(action):
            if found.get("truncated"):
                raise ValidationError(
                    f"{where} holds more entries than GitHub can list. Split it into folders."
                )
            kinds = {"tree": "dir", "blob": "file"}
            entries = {
                _nfc(item["path"]): _Entry(
                    kind=kinds.get(item["type"], "other"), sha=item["sha"], stored=item["path"]
                )
                for item in found["tree"]
            }
        _remember(self._trees, sha, entries)
        return entries

    def _listing(self, snapshot: _Snapshot, path: LogicalPath) -> dict[str, _Entry] | None:
        """The folder at ``path`` in ``snapshot``, or ``None`` if it doesn't exist."""
        entries = self._tree(snapshot.tree, "the repository root")
        walked: list[str] = []
        prefix_depth = len(self._address.prefix)
        for depth, part in enumerate((*self._address.prefix, *path.parts)):
            walked.append(part)
            entry = entries.get(part)
            if depth < prefix_depth:
                # The prefix gets the same name checks as containers, or a
                # look-alike prefix would be created beside the real one.
                self._refuse_other_form(entry, part)
                twin = _twin(entries, part)
                if entry is None and twin is not None:
                    raise ValidationError(
                        f"The prefix folder {part!r} differs only in case from {twin!r}, "
                        "which already exists. Use the existing spelling in the URI."
                    )
            if entry is None or entry.stored != part:
                return None  # missing, or committed in another Unicode form
            if entry.kind != "dir":
                raise ValidationError(f"{'/'.join(walked)} is a file, not a folder.")
            entries = self._tree(entry.sha, "/".join(walked))
        return entries

    def _folder(self, snapshot: _Snapshot, path: LogicalPath) -> dict[str, _Entry]:
        """The folder at ``path``. Raises ``NotFoundError`` if it doesn't exist.

        The destination root always exists, even before anything is written
        under the prefix.
        """
        self._check_names(path)
        entries = self._listing(snapshot, path)
        if entries is not None:
            return entries
        if not path.parts:
            return {}  # the prefix isn't created yet
        # Explain why: a name that differs only in case is the usual reason.
        parent = self._listing(snapshot, path.parent)
        if parent is not None:
            self._refuse_other_form(parent.get(path.name), path.name)
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
        _remember(self._blobs, sha, data)
        return data

    # Writing one commit

    def _commit(
        self, snapshot: _Snapshot, changes: Mapping[str, bytes], message: str, action: str
    ) -> str:
        """Commit ``changes`` (``git path -> bytes``) on top of ``snapshot``.

        Moves the branch only as a fast forward from ``snapshot``; raises
        ``_BranchMoved`` otherwise, and nothing lands. Returns the commit ID.
        """
        items = []
        for git_path, data in sorted(changes.items()):
            item: dict[str, Any] = {"path": git_path, "mode": "100644", "type": "blob"}
            text = None
            if b"\0" not in data:
                with contextlib.suppress(UnicodeDecodeError):
                    text = data.decode("utf-8")
            if text is not None:
                item["content"] = text  # the tree API takes text inline
            else:
                blob = self._request(
                    "POST",
                    f"{self._repo_url}/git/blobs",
                    action,
                    body={"content": base64.b64encode(data).decode(), "encoding": "base64"},
                )
                with _network_errors(action):
                    item["sha"] = str(blob["sha"])
            items.append(item)
        tree = self._request(
            "POST",
            f"{self._repo_url}/git/trees",
            action,
            body={"base_tree": snapshot.tree, "tree": items},
        )
        with _network_errors(action):
            tree_sha = str(tree["sha"])
        commit = self._request(
            "POST",
            f"{self._repo_url}/git/commits",
            action,
            body={"message": message, "tree": tree_sha, "parents": [snapshot.commit]},
        )
        with _network_errors(action):
            commit_sha = str(commit["sha"])
        try:
            self._request(
                "PATCH",
                f"{self._repo_url}/git/refs/{self._ref_path}",
                action,
                body={"sha": commit_sha, "force": False},
            )
        except NotFoundError:
            raise NotFoundError(self._branch_gone()) from None
        _remember(self._commit_trees, commit_sha, tree_sha)
        _remember(self._replaced, snapshot.commit, None)
        if self._latest is not None:
            _remember(self._replaced, self._latest, None)
        self._latest = commit_sha
        self._landed_at = time.monotonic()
        return commit_sha

    def _with_retries(self, action: str, attempt: Callable[[], _T]) -> _T:
        """Run ``attempt`` (read the head, decide, commit) again while the branch
        keeps moving under it, waiting a little longer each time."""
        for number in range(_BRANCH_RETRIES):
            try:
                return attempt()
            except _BranchMoved as error:
                if number == _BRANCH_RETRIES - 1:
                    raise BackendUnavailableError(str(error)) from None
                delay = _BRANCH_BACKOFF * 2**number
                time.sleep(delay + random.uniform(0, delay))
        raise AssertionError("unreachable")

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

    def _is_records_file(
        self, path: LogicalPath, entries: Mapping[str, _Entry], index: _Index, name: str
    ) -> bool:
        """Whether ``name`` is the ``.json`` or ``.md`` of a record here.

        A ``.json`` MetaRelay didn't index counts only if ``list_records``
        would list it (it holds a record with that ID): a hand-committed
        ``config.json`` is a file.
        """
        for record_id in self._record_ids(entries, index):
            if _fold(name) not in {_fold(f"{record_id}.json"), _fold(f"{record_id}.md")}:
                continue
            if record_id in index.records:
                return True
            entry = entries.get(f"{record_id}.json")
            if entry is not None and entry.kind == "file":
                data = self._blob(entry.sha, f"read {record_id!r} in {self._describe(path)}")
                record = _parse_record(data)
                if record is not None and record.record_id == record_id:
                    return True
        return False

    # Checks

    def _describe(self, path: LogicalPath) -> str:
        return f"container {str(path)!r}" if path.parts else "the destination root"

    def _check_names(self, path: LogicalPath) -> None:
        for part in path.parts:
            check_local_name(part, "Container name")

    def _refuse_other_form(self, entry: _Entry | None, name: str) -> None:
        """Refuse a name committed in another Unicode form (NFD), rather than
        reading a path that isn't there or committing a second, look-alike copy."""
        if entry is not None and entry.stored and entry.stored != name:
            raise ValidationError(
                f"{name!r} is committed at GitHub in a different Unicode form (NFD) than "
                "MetaRelay uses. MetaRelay can't open it yet: rename it on GitHub (retype "
                "the name), or use another name."
            )

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
        record = _parse_record(data)
        return record_checksum(record) if record is not None else "unreadable:" + _sha256(data)

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
        try:
            session._head()
        except NotFoundError:
            raise ConfigurationError(
                f"{full_name} has no branch {address.branch!r}. Create it first (an empty "
                "repository needs a first commit)."
            ) from None

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
