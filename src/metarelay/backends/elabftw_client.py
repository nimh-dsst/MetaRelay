"""The adapter between the eLabFTW backend and ``elabapi-python`` (plan R2).

This is the only module that imports the generated client. The backend
calls ``ElabClient`` with plain values and gets plain ``dict``s back, and
every error leaves here as a ``metarelay`` error. When the generated
client changes between eLabFTW releases, this is the file to update.

Entries are read from the raw JSON response, not the generated models: the
``Metadata`` model drops keys it doesn't know, including ``metarelay``.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import elabapi_python
import urllib3
from elabapi_python.rest import ApiException

from metarelay.core.errors import (
    AuthenticationError,
    BackendUnavailableError,
    MetaRelayError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)

__all__ = ["ElabClient", "Entity"]

Entity = Literal["experiments", "items"]
"""The two kinds of eLabFTW entry. The API calls resources ``items``."""

PAGE_SIZE = 100
SCOPE_TEAM = 2
"""Entries of the API key's team. Without a scope, eLabFTW uses the user's
own display setting, which may hide teammates' entries; "everything" would
also match other teams' MetaRelay entries that the user can only read."""

_STATUS_ERRORS: dict[int, type[MetaRelayError]] = {
    400: ValidationError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
}


def _message(error: ApiException) -> str:
    """eLabFTW's own error text. It never contains the API key."""
    try:
        body = json.loads(error.body or b"{}")
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    parts = [str(body[key]) for key in ("message", "description") if body.get(key)]
    return ": " + " ".join(parts) if parts else ""


@contextlib.contextmanager
def _errors(action: str) -> Iterator[None]:
    """Translate client and network errors into metarelay errors.

    Nothing is chained: urllib3 errors can carry the request, and the
    request carries the API key.
    """
    try:
        yield
    except MetaRelayError:
        raise
    except ApiException as error:
        error_class = _STATUS_ERRORS.get(error.status or 0, BackendUnavailableError)
        raise error_class(
            f"eLabFTW could not {action} (HTTP {error.status}){_message(error)}"
        ) from None
    except urllib3.exceptions.HTTPError as error:
        raise BackendUnavailableError(
            f"Could not reach eLabFTW to {action} ({type(error).__name__})."
        ) from None
    except ValueError:
        raise BackendUnavailableError(
            f"eLabFTW sent a response MetaRelay did not understand while trying to {action}."
        ) from None


def _id_from_location(headers: Any) -> int:
    location = headers.get("Location") or headers.get("location")
    if not location:
        raise ValueError("no Location header")
    return int(location.rstrip("/").rsplit("/", 1)[1])


class ElabClient:
    """The eLabFTW calls the backend needs, for one entity type."""

    def __init__(self, api_url: str, token: str, *, entity: Entity, verify_tls: bool) -> None:
        configuration = elabapi_python.Configuration()
        configuration.host = api_url
        configuration.verify_ssl = verify_tls
        configuration.api_key["api_key"] = token
        configuration.api_key_prefix["api_key"] = ""
        self._client = elabapi_python.ApiClient(configuration)
        self._client.set_default_header("Authorization", token)
        self.entity: Entity = entity
        if entity == "experiments":
            api = elabapi_python.ExperimentsApi(self._client)
            self._list, self._get = api.read_experiments, api.get_experiment
            self._post, self._patch = api.post_experiment_with_http_info, api.patch_experiment
        else:
            api = elabapi_python.ItemsApi(self._client)
            self._list, self._get = api.read_items, api.get_item
            self._post, self._patch = api.post_item_with_http_info, api.patch_item
        self._uploads = elabapi_python.UploadsApi(self._client)
        self._users = elabapi_python.UsersApi(self._client)

    def check_login(self) -> None:
        """Fail now, with a clear error, if the key is wrong."""
        with _errors("log in"):
            self._users.read_user("me", _preload_content=False)

    def search(self, extended: str) -> list[dict[str, Any]]:
        """Every entry matching an extended search query (no ``metadata``)."""
        found: list[dict[str, Any]] = []
        with _errors("search entries"):
            while True:
                response = self._list(
                    extended=extended,
                    scope=SCOPE_TEAM,
                    # A stable order, so paging by offset can't skip an entry
                    # that another writer changes meanwhile.
                    order="id",
                    sort="asc",
                    limit=PAGE_SIZE,
                    offset=len(found),
                    _preload_content=False,
                )
                page = json.loads(response.data)
                found.extend(page)
                if len(page) < PAGE_SIZE:
                    return found

    def get(self, entry_id: int) -> dict[str, Any]:
        """One entry, with ``metadata`` decoded to a ``dict`` (``{}`` if none)."""
        with _errors(f"read entry {entry_id}"):
            entry = json.loads(self._get(entry_id, _preload_content=False).data)
            raw = entry.get("metadata")
            entry["metadata"] = json.loads(raw) if isinstance(raw, str) and raw else raw or {}
            if not isinstance(entry["metadata"], dict):
                raise ValueError("metadata is not an object")
            return entry

    def create(self, title: str, tags: list[str], body: str, metadata: dict[str, Any]) -> int:
        """Create an entry with everything set in one request."""
        payload = {"title": title, "tags": tags, "body": body, "metadata": json.dumps(metadata)}
        with _errors(f"create entry {title!r}"):
            _, _, headers = self._post(body=payload)
            return _id_from_location(headers)

    def update(self, entry_id: int, *, body: str, metadata: dict[str, Any]) -> None:
        """Replace an entry's body and metadata together, in one request."""
        with _errors(f"update entry {entry_id}"):
            self._patch({"body": body, "metadata": json.dumps(metadata)}, entry_id)

    def uploads(self, entry_id: int) -> list[dict[str, Any]]:
        """The entry's current (not deleted or archived) uploads."""
        with _errors(f"list the uploads of entry {entry_id}"):
            response = self._uploads.read_uploads(self.entity, entry_id, _preload_content=False)
            return json.loads(response.data)

    def upload(self, entry_id: int, name: str, data: bytes, comment: str) -> int:
        """Attach ``data`` as a file named ``name``."""
        with tempfile.TemporaryDirectory() as directory:
            # The upload takes its name from the file, so the file needs it.
            path = Path(directory) / name
            try:
                path.write_bytes(data)
            except OSError as error:
                raise ValidationError(
                    f"File name {name!r} can't be used: {error.strerror or type(error).__name__}."
                ) from None
            with _errors(f"upload {name!r} to entry {entry_id}"):
                _, _, headers = self._uploads.post_upload_with_http_info(
                    self.entity, entry_id, file=str(path), comment=comment
                )
                return _id_from_location(headers)

    def delete_upload(self, entry_id: int, upload_id: int) -> None:
        with _errors(f"remove upload {upload_id} from entry {entry_id}"):
            self._uploads.delete_upload(self.entity, entry_id, upload_id)

    def close(self) -> None:
        # The generated ApiClient has no close(); drop its pooled connections.
        with contextlib.suppress(AttributeError):
            self._client.rest_client.pool_manager.clear()
