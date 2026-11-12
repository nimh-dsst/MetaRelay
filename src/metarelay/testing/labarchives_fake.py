"""A stateful fake LabArchives API server, for tests and the dev stack.

LabArchives has no guest account we can rely on (risk R1), so the backend is
built against this fake. It speaks the same HTTP as LabArchives for the calls
``labapi`` makes (``/api/<class>/<method>``), so the real ``labapi`` client
runs against it unchanged: point ``labapi.Client(base_url=...)`` (or the
``API_URL`` environment variable) at it.

What it does:

- keeps a notebook tree (notebooks, folders, pages) and page entries (rich
  text and attachments) per user, in memory;
- stubs the browser login: ``/api_user_login`` redirects straight back to the
  ``redirect_uri`` with ``email`` and ``auth_code``, as LabArchives does after
  a successful login;
- checks the HMAC ``sig`` on every call (turn off with ``verify_signatures``);
- answers errors the way LabArchives does: a non-200 status with an
  ``<error-code>`` body, which ``labapi`` turns into its own exceptions.

Use it in tests::

    fake = FakeLabArchives(akid="id", akpass="secret")
    fake.add_user("ada@example.org", auth_code="code", notebooks=["Lab Notebook"])
    with serve_fake(fake) as base_url:
        client = labapi.Client(base_url, "id", "secret")

or run it on its own (the ``docker compose`` dev stack does this)::

    python -m metarelay.testing.labarchives_fake --port 8080 --seed seed.json

The fake implements only what ``labapi`` uses, and its error codes and
element names come from reading ``labapi``'s parser. Responses DSST captures
from the real service (``python -m metarelay.testing.labarchives_smoke``) are the check on
it: wherever they disagree, the fake is wrong.

Standard library only, so it runs without ``labapi`` installed.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import itertools
import json
import mimetypes
import os
import re
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.sax.saxutils import escape

__all__ = ["FakeLabArchives", "FakeLabArchivesError", "main", "serve_fake"]

ROOT_TREE_ID = "0"
"""The tree ID ``labapi`` uses for a notebook's top level."""

# The error codes labapi treats as authentication failures.
INVALID_AKID = 4506
BAD_LOGIN = 4514
BAD_SIGNATURE = 4520
SESSION_EXPIRED = 4533
# Codes for everything else. LabArchives' own numbers for these are not
# documented; captured responses should replace them.
NOT_FOUND = 4404
BAD_REQUEST = 4400
# Documented by labapi: the signed-in account is not the notebook's owner.
NOT_OWNER = 4547


class FakeLabArchivesError(Exception):
    """An error the fake answers with, as LabArchives would."""

    def __init__(self, code: int, description: str, status: HTTPStatus) -> None:
        super().__init__(description)
        self.code = code
        self.description = description
        self.status = status


def _not_found(what: str) -> FakeLabArchivesError:
    return FakeLabArchivesError(NOT_FOUND, f"{what} not found", HTTPStatus.NOT_FOUND)


@dataclass
class _Node:
    tree_id: str
    notebook_id: str
    parent_id: str
    name: str
    is_page: bool


@dataclass
class _Entry:
    eid: str
    page_id: str
    part_type: str
    data: str
    """The text of a text entry, or the caption of an attachment."""
    filename: str = ""
    content_type: str = ""
    content: bytes = b""
    version: int = 1


@dataclass
class _User:
    uid: str
    email: str
    auth_code: str
    notebook_ids: list[str] = field(default_factory=list)


class FakeLabArchives:
    """The fake's state, and the code that answers each API call.

    Thread-safe: ``serve_fake`` runs one thread per request.
    """

    def __init__(
        self,
        *,
        akid: str = "fake-akid",
        akpass: str = "fake-akpass",
        verify_signatures: bool = True,
    ) -> None:
        self.akid = akid
        self.akpass = akpass
        self.verify_signatures = verify_signatures
        self.login_email: str | None = None
        """Who the login page logs in. Defaults to the first user added."""
        self.calls: list[tuple[str, str]] = []
        """Every API call answered, as ``(class, method)``, for tests to inspect."""
        self.max_file_size = 100 * 1024 * 1024
        self.strip_html_comments = False
        """Remove ``<!-- ... -->`` from text entries when saving them, as
        LabArchives may (unconfirmed; the smoke test checks it live)."""
        self._users: dict[str, _User] = {}
        self._notebooks: dict[str, str] = {}
        self._nodes: dict[str, _Node] = {}
        self._entries: dict[str, _Entry] = {}
        self._ids = itertools.count(1)
        self._failures: dict[str, FakeLabArchivesError] = {}
        self._lock = threading.RLock()

    # Setting up state

    def add_user(
        self, email: str, *, auth_code: str, notebooks: Sequence[str] = ("Lab Notebook",)
    ) -> str:
        """Add a user who can log in with ``auth_code``. Returns their user ID."""
        with self._lock:
            user = _User(uid=f"U{next(self._ids)}", email=email, auth_code=auth_code)
            self._users[email] = user
            if self.login_email is None:
                self.login_email = email
            for name in notebooks:
                self.add_notebook(email, name)
            return user.uid

    def add_notebook(self, email: str, name: str) -> str:
        """Give ``email`` a new notebook. Returns its notebook ID."""
        with self._lock:
            nbid = f"NB{next(self._ids)}"
            self._notebooks[nbid] = name
            self._users[email].notebook_ids.append(nbid)
            return nbid

    def add_node(self, notebook_id: str, parent_id: str, name: str, *, is_page: bool) -> str:
        """Add a folder or page. ``parent_id`` is ``"0"`` for the top level."""
        with self._lock:
            if parent_id != ROOT_TREE_ID:
                parent = self._nodes.get(parent_id)
                if parent is None or parent.notebook_id != notebook_id or parent.is_page:
                    raise _not_found(f"Folder {parent_id}")
            tree_id = f"{notebook_id}_{next(self._ids)}"
            self._nodes[tree_id] = _Node(tree_id, notebook_id, parent_id, name, is_page)
            return tree_id

    def add_text_entry(self, page_id: str, html: str) -> str:
        """Add a rich-text entry to a page. Returns its entry ID."""
        with self._lock:
            self._page(page_id)
            eid = f"E{next(self._ids)}"
            self._entries[eid] = _Entry(eid, page_id, "text entry", html)
            return eid

    def add_attachment(self, page_id: str, filename: str, content: bytes, caption: str) -> str:
        """Add an attachment entry to a page. Returns its entry ID."""
        with self._lock:
            self._page(page_id)
            eid = f"E{next(self._ids)}"
            self._entries[eid] = _Entry(
                eid,
                page_id,
                "Attachment",
                caption,
                filename=filename,
                content_type=_guess_type(filename),
                content=content,
            )
            return eid

    @classmethod
    def from_seed(cls, path: str | Path, **options: Any) -> FakeLabArchives:
        """Build a fake from a JSON fixture file.

        The file holds ``{"users": [...]}``. Each user has ``email``,
        ``auth_code``, and ``notebooks``; each notebook has a ``name`` and a
        ``tree`` of ``{"folder": name, "children": [...]}`` and
        ``{"page": name, "entries": [...]}`` items. An entry is
        ``{"text": html}`` or ``{"attachment": {"filename", "caption",
        "content"}}`` (``content`` is text; use ``content_base64`` for bytes).
        """
        seed = json.loads(Path(path).read_text())
        fake = cls(**options)
        for user in seed.get("users", []):
            fake.add_user(user["email"], auth_code=user["auth_code"], notebooks=())
            for notebook in user.get("notebooks", []):
                nbid = fake.add_notebook(user["email"], notebook["name"])
                fake._seed_tree(nbid, ROOT_TREE_ID, notebook.get("tree", []))
        return fake

    def _seed_tree(self, nbid: str, parent_id: str, items: list[dict[str, Any]]) -> None:
        for item in items:
            if "folder" in item:
                folder_id = self.add_node(nbid, parent_id, item["folder"], is_page=False)
                self._seed_tree(nbid, folder_id, item.get("children", []))
                continue
            page_id = self.add_node(nbid, parent_id, item["page"], is_page=True)
            for entry in item.get("entries", []):
                if "text" in entry:
                    self.add_text_entry(page_id, entry["text"])
                    continue
                attachment = entry["attachment"]
                if "content_base64" in attachment:
                    content = base64.b64decode(attachment["content_base64"])
                else:
                    content = attachment.get("content", "").encode()
                self.add_attachment(
                    page_id, attachment["filename"], content, attachment.get("caption", "")
                )

    # Test hooks: changes made "in the LabArchives web app"

    def edit_entry(self, eid: str, data: str) -> None:
        """Change an entry's text (or an attachment's caption), as a person would."""
        with self._lock:
            entry = self._entry(eid)
            entry.data = data
            entry.version += 1

    def replace_attachment(self, eid: str, content: bytes) -> None:
        """Replace an attachment's bytes, as a person uploading a new version would."""
        with self._lock:
            entry = self._entry(eid)
            entry.content = content
            entry.version += 1

    def delete_entry(self, eid: str) -> None:
        """Delete an entry, as a person would in the web app (labapi can't)."""
        with self._lock:
            self._entry(eid)
            del self._entries[eid]

    def fail_next(
        self,
        method: str,
        code: int,
        description: str,
        status: HTTPStatus = HTTPStatus.FORBIDDEN,
    ) -> None:
        """Answer the next call to API ``method`` (``insert_node``, say) with an error."""
        with self._lock:
            self._failures[method] = FakeLabArchivesError(code, description, status)

    def entries_on(self, page_id: str) -> list[dict[str, Any]]:
        """A page's entries as plain dicts, for tests to inspect."""
        with self._lock:
            return [
                {
                    "eid": entry.eid,
                    "part_type": entry.part_type,
                    "data": entry.data,
                    "filename": entry.filename,
                    "content": entry.content,
                }
                for entry in self._entries.values()
                if entry.page_id == page_id
            ]

    # Answering requests

    def handle(
        self, http_method: str, path: str, query: Mapping[str, str], body: bytes, content_type: str
    ) -> tuple[HTTPStatus, dict[str, str], bytes]:
        """Answer one HTTP request. Returns status, headers, and body."""
        try:
            if path.rstrip("/").endswith("/api_user_login"):
                return self._login_page(query)
            parts = path.strip("/").split("/")
            if len(parts) < 3 or parts[-3] != "api":
                raise _not_found(f"Path {path}")
            api_class, method = parts[-2], parts[-1]
            self._check_signature(query, method)
            with self._lock:
                self.calls.append((api_class, method))
                failure = self._failures.pop(method, None)
                if failure is not None:
                    raise failure
                if content_type.startswith("application/x-www-form-urlencoded"):
                    form = dict(parse_qsl(body.decode(), keep_blank_values=True))
                else:
                    form = {}
                return self._dispatch(api_class, method, query, form, body)
        except FakeLabArchivesError as error:
            xml = (
                f"<error><error-code>{error.code}</error-code>"
                f"<error-description>{escape(error.description)}</error-description></error>"
            )
            return error.status, {"Content-Type": "application/xml"}, xml.encode()

    def _saved_text(self, html: str) -> str:
        return re.sub(r"<!--.*?-->", "", html, flags=re.S) if self.strip_html_comments else html

    def _check_signature(self, query: Mapping[str, str], signed: str) -> None:
        if not self.verify_signatures:
            return
        if query.get("akid") != self.akid:
            raise FakeLabArchivesError(INVALID_AKID, "Invalid akid", HTTPStatus.UNAUTHORIZED)
        expires = query.get("expires", "")
        expected = base64.b64encode(
            hmac.new(
                self.akpass.encode(), f"{self.akid}{signed}{expires}".encode(), hashlib.sha512
            ).digest()
        ).decode()
        if not hmac.compare_digest(expected, query.get("sig", "")):
            raise FakeLabArchivesError(BAD_SIGNATURE, "Invalid signature", HTTPStatus.UNAUTHORIZED)
        if not expires.isdigit() or int(expires) < time.time() * 1000:
            raise FakeLabArchivesError(SESSION_EXPIRED, "Request expired", HTTPStatus.UNAUTHORIZED)

    def _login_page(self, query: Mapping[str, str]) -> tuple[HTTPStatus, dict[str, str], bytes]:
        redirect = query.get("redirect_uri", "")
        if not redirect:
            raise FakeLabArchivesError(BAD_REQUEST, "redirect_uri is required", HTTPStatus(400))
        self._check_signature(query, redirect)
        with self._lock:
            user = self._users.get(self.login_email or "")
            if user is None:
                raise FakeLabArchivesError(BAD_LOGIN, "No user to log in", HTTPStatus(400))
            params = {"email": user.email, "auth_code": user.auth_code}
        parts = urlsplit(redirect)
        query_string = "&".join(filter(None, [parts.query, urlencode(params)]))
        location = urlunsplit(parts._replace(query=query_string))
        return HTTPStatus.FOUND, {"Location": location}, b""

    def _dispatch(
        self,
        api_class: str,
        method: str,
        query: Mapping[str, str],
        form: Mapping[str, str],
        body: bytes,
    ) -> tuple[HTTPStatus, dict[str, str], bytes]:
        if (api_class, method) == ("users", "user_access_info"):
            return _xml(self._user_access_info(query))
        user = self._user_by_uid(query.get("uid", ""))
        match api_class, method:
            case "users", "max_file_size":
                return _xml(f"<users><max-file-size>{self.max_file_size}</max-file-size></users>")
            case "notebooks", "create_notebook":
                nbid = self.add_notebook(user.email, query.get("name", "") or "Untitled")
                return _xml(f"<notebooks><nbid>{nbid}</nbid></notebooks>")
            case "notebooks", "modify_notebook_info":
                nbid = self._notebook(user, query.get("nbid", ""))
                self._notebooks[nbid] = query.get("name", "")
                return _xml(f"<notebooks><nbid>{nbid}</nbid></notebooks>")
            case "tree_tools", "get_tree_level":
                return _xml(self._tree_level(user, query))
            case "tree_tools", "insert_node":
                return _xml(self._insert_node(user, query))
            case "tree_tools", "update_node":
                return _xml(self._update_node(user, query))
            case "tree_tools", "get_entries_for_page":
                return _xml(self._entries_for_page(user, query))
            case "entries", "add_entry":
                return _xml(self._add_entry(user, query, form))
            case "entries", "update_entry":
                entry = self._owned_entry(user, query.get("eid", ""))
                entry.data = self._saved_text(form.get("entry_data", ""))
                entry.version += 1
                return _xml(_entry_xml(entry))
            case "entries", "add_attachment":
                return _xml(self._add_attachment(user, query, body))
            case "entries", "update_attachment":
                entry = self._owned_entry(user, query.get("eid", ""))
                entry.filename = query.get("filename", "") or entry.filename
                entry.content_type = _guess_type(entry.filename)
                entry.data = query.get("caption", "")
                entry.content = body
                entry.version += 1
                return _xml(_entry_xml(entry))
            case "entries", "entry_attachment":
                entry = self._owned_entry(user, query.get("eid", ""))
                if entry.part_type != "Attachment":
                    raise _not_found(f"Attachment {entry.eid}")
                filename = entry.filename.replace('"', "")
                headers = {
                    "Content-Type": entry.content_type,
                    "Content-Disposition": f'attachment; filename="{filename}"',
                }
                return HTTPStatus.OK, headers, entry.content
        raise _not_found(f"API method {api_class}/{method}")

    def _user_access_info(self, query: Mapping[str, str]) -> str:
        user = self._users.get(query.get("login_or_email", ""))
        if user is None or not hmac.compare_digest(user.auth_code, query.get("password", "")):
            raise FakeLabArchivesError(
                BAD_LOGIN, "Login or password incorrect", HTTPStatus.UNAUTHORIZED
            )
        notebooks = "".join(
            f"<notebook><id>{nbid}</id><name>{escape(self._notebooks[nbid])}</name>"
            f'<is-default type="boolean">{"true" if i == 0 else "false"}</is-default></notebook>'
            for i, nbid in enumerate(user.notebook_ids)
        )
        return f'<users><id>{user.uid}</id><notebooks type="array">{notebooks}</notebooks></users>'

    def _tree_level(self, user: _User, query: Mapping[str, str]) -> str:
        nbid = self._notebook(user, query.get("nbid", ""))
        parent_id = query.get("parent_tree_id", "") or ROOT_TREE_ID
        if parent_id != ROOT_TREE_ID:
            self._folder(nbid, parent_id)
        nodes = "".join(
            f'<level-node><is-page type="boolean">{str(node.is_page).lower()}</is-page>'
            f"<tree-id>{node.tree_id}</tree-id>"
            f"<display-text>{escape(node.name)}</display-text></level-node>"
            for node in self._nodes.values()
            if node.notebook_id == nbid and node.parent_id == parent_id
        )
        return f'<tree-tools><level-nodes type="array">{nodes}</level-nodes></tree-tools>'

    def _insert_node(self, user: _User, query: Mapping[str, str]) -> str:
        nbid = self._notebook(user, query.get("nbid", ""))
        name = query.get("display_text", "")
        if not name.strip():
            raise FakeLabArchivesError(BAD_REQUEST, "display_text is required", HTTPStatus(400))
        is_folder = query.get("is_folder", "").lower() == "true"
        parent_id = query.get("parent_tree_id", "") or ROOT_TREE_ID
        tree_id = self.add_node(nbid, parent_id, name, is_page=not is_folder)
        return (
            f"<tree-tools><node><tree-id>{tree_id}</tree-id>"
            f"<display-text>{escape(name)}</display-text>"
            f'<is-page type="boolean">{str(not is_folder).lower()}</is-page></node></tree-tools>'
        )

    def _update_node(self, user: _User, query: Mapping[str, str]) -> str:
        nbid = self._notebook(user, query.get("nbid", ""))
        node = self._nodes.get(query.get("tree_id", ""))
        if node is None or node.notebook_id != nbid:
            raise _not_found(f"Node {query.get('tree_id')}")
        if query.get("display_text"):
            node.name = query["display_text"]
        if query.get("parent_tree_id"):
            new_parent = query["parent_tree_id"]
            if new_parent != ROOT_TREE_ID:
                self._folder(nbid, new_parent)
            node.parent_id = new_parent
        return f"<tree-tools><node><tree-id>{node.tree_id}</tree-id></node></tree-tools>"

    def _entries_for_page(self, user: _User, query: Mapping[str, str]) -> str:
        page = self._owned_page(user, query.get("page_tree_id", ""))
        entries = "".join(
            _entry_xml(entry) for entry in self._entries.values() if entry.page_id == page.tree_id
        )
        return f'<tree-tools><entries type="array">{entries}</entries></tree-tools>'

    def _add_entry(self, user: _User, query: Mapping[str, str], form: Mapping[str, str]) -> str:
        page = self._owned_page(user, query.get("pid", ""))
        eid = f"E{next(self._ids)}"
        part_type = query.get("part_type", "") or "text entry"
        data = form.get("entry_data", "")
        if part_type == "text entry":
            data = self._saved_text(data)
        self._entries[eid] = _Entry(eid, page.tree_id, part_type, data)
        return _entry_xml(self._entries[eid], wrap="entries")

    def _add_attachment(self, user: _User, query: Mapping[str, str], body: bytes) -> str:
        page = self._owned_page(user, query.get("pid", ""))
        filename = query.get("filename", "")
        if not filename:
            raise FakeLabArchivesError(BAD_REQUEST, "filename is required", HTTPStatus(400))
        if len(body) > self.max_file_size:
            raise FakeLabArchivesError(BAD_REQUEST, "File too large", HTTPStatus(413))
        eid = self.add_attachment(page.tree_id, filename, body, query.get("caption", ""))
        return _entry_xml(self._entries[eid], wrap="entries")

    # Lookups

    def _user_by_uid(self, uid: str) -> _User:
        for user in self._users.values():
            if user.uid == uid:
                return user
        raise FakeLabArchivesError(SESSION_EXPIRED, "Unknown user", HTTPStatus.UNAUTHORIZED)

    def _notebook(self, user: _User, nbid: str) -> str:
        if nbid not in user.notebook_ids:
            raise _not_found(f"Notebook {nbid}")
        return nbid

    def _folder(self, nbid: str, tree_id: str) -> _Node:
        node = self._nodes.get(tree_id)
        if node is None or node.notebook_id != nbid or node.is_page:
            raise _not_found(f"Folder {tree_id}")
        return node

    def _page(self, tree_id: str) -> _Node:
        node = self._nodes.get(tree_id)
        if node is None or not node.is_page:
            raise _not_found(f"Page {tree_id}")
        return node

    def _owned_page(self, user: _User, tree_id: str) -> _Node:
        page = self._page(tree_id)
        self._notebook(user, page.notebook_id)
        return page

    def _entry(self, eid: str) -> _Entry:
        entry = self._entries.get(eid)
        if entry is None:
            raise _not_found(f"Entry {eid}")
        return entry

    def _owned_entry(self, user: _User, eid: str) -> _Entry:
        entry = self._entry(eid)
        self._owned_page(user, entry.page_id)
        return entry


def _guess_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _entry_xml(entry: _Entry, *, wrap: str | None = None) -> str:
    xml = (
        f"<entry><eid>{entry.eid}</eid>"
        f"<part-type>{escape(entry.part_type)}</part-type>"
        f"<attach-file-name>{escape(entry.filename)}</attach-file-name>"
        f"<attach-content-type>{escape(entry.content_type)}</attach-content-type>"
        f"<entry-data>{escape(entry.data)}</entry-data>"
        f'<version type="integer">{entry.version}</version></entry>'
    )
    return f"<{wrap}>{xml}</{wrap}>" if wrap else xml


def _xml(body: str) -> tuple[HTTPStatus, dict[str, str], bytes]:
    document = '<?xml version="1.0" encoding="UTF-8"?>' + body
    return HTTPStatus.OK, {"Content-Type": "application/xml; charset=utf-8"}, document.encode()


# Serving over HTTP


class _Handler(BaseHTTPRequestHandler):
    fake: FakeLabArchives
    server_version = "FakeLabArchives/1"

    def do_GET(self) -> None:
        self._answer("GET")

    def do_POST(self) -> None:
        self._answer("POST")

    def _answer(self, method: str) -> None:
        parts = urlsplit(self.path)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        status, headers, body = self.fake.handle(
            method, parts.path, query, self._read_body(), self.headers.get("Content-Type", "")
        )
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("FAKE_LABARCHIVES_LOG"):
            super().log_message(format, *args)


def _server(fake: FakeLabArchives, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"fake": fake})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


@contextlib.contextmanager
def serve_fake(fake: FakeLabArchives, host: str = "127.0.0.1", port: int = 0) -> Iterator[str]:
    """Serve ``fake`` in a background thread. Yields its base URL."""
    server = _server(fake, host, port)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def main(argv: Sequence[str] | None = None) -> None:
    """Run the fake until interrupted. Settings also come from ``FAKE_LABARCHIVES_*``."""
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=env("FAKE_LABARCHIVES_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("FAKE_LABARCHIVES_PORT", "8080")))
    parser.add_argument("--seed", default=env("FAKE_LABARCHIVES_SEED"), help="JSON fixture file")
    parser.add_argument("--akid", default=env("FAKE_LABARCHIVES_AKID", "fake-akid"))
    parser.add_argument("--akpass", default=env("FAKE_LABARCHIVES_AKPASS", "fake-akpass"))
    parser.add_argument("--no-verify", action="store_true", help="accept any akid and signature")
    args = parser.parse_args(argv)

    options = {"akid": args.akid, "akpass": args.akpass, "verify_signatures": not args.no_verify}
    if args.seed:
        fake = FakeLabArchives.from_seed(args.seed, **options)
    else:
        fake = FakeLabArchives(**options)
        fake.add_user("demo@example.org", auth_code="demo", notebooks=["Lab Notebook"])
    server = _server(fake, args.host, args.port)
    print(f"Fake LabArchives on http://{args.host}:{server.server_address[1]}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    main()
