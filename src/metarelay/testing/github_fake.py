"""A stateful fake of the GitHub REST API, for tests and the dev stack.

It answers the calls the ``github://`` backend makes, with GitHub's status
codes and JSON shapes, so the real ``httpx`` client runs against it
unchanged: point ``GitHubConfig(api_url=...)`` (or
``METARELAY_GITHUB_API_URL``) at it.

What it does:

- keeps each repository as Git does: blobs, trees, and commits stored by
  ID, and branches that point at commits. Folders exist only because files
  are in them;
- checks the bearer token on every call (401 ``Bad credentials`` otherwise);
- answers ``GET /repos/{owner}/{repo}`` and the Git Data API calls the
  backend makes: ``GET .../git/ref/heads/{branch}``,
  ``PATCH .../git/refs/heads/{branch}``, ``GET``/``POST .../git/commits``,
  ``GET``/``POST .../git/trees`` (``base_tree``, nested paths, inline
  ``content``) and ``GET``/``POST .../git/blobs``;
- refuses a ref update that isn't a fast forward (422, as GitHub does), so
  a commit made on a branch that moved since it was read never lands;
- uses Git's real blob SHA-1, so blob ``sha`` values match what GitHub
  would report for the same bytes. Tree and commit IDs are only unique.

Use it in tests::

    fake = FakeGitHub(token="test-token")
    fake.add_repo("my-lab/metadata")
    with serve_fake(fake) as api_url:
        session = connect(
            "github://my-lab/metadata@main/records",
            GitHubConfig(token="test-token", api_url=api_url),
        )

or run it on its own::

    python -m metarelay.testing.github_fake --port 8081 --repo my-lab/metadata

``fail_next`` queues error responses (a 5xx, a rate limit) for the next
requests, to test error handling. ``put_file`` and ``delete_file`` change
a branch the way a person on github.com would.

The fake implements only what the backend uses. Where it and GitHub
disagree, the fake is wrong.

Standard library only, so it runs without ``httpx`` installed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import json
import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

__all__ = ["FakeGitHub", "FakeRepo", "git_blob_sha", "serve_fake"]

FILE_MODE = "100644"
TREE_MODE = "040000"


def git_blob_sha(data: bytes) -> str:
    """Git's object ID for a blob: SHA-1 over ``blob <size>\\0<data>``."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _b64(data: bytes) -> str:
    # GitHub wraps base64 content at 60 characters; clients must cope.
    text = base64.b64encode(data).decode()
    return "\n".join(text[i : i + 60] for i in range(0, len(text), 60)) + "\n"


@dataclass
class Commit:
    branch: str
    message: str
    sha: str
    paths: frozenset[str]
    """The files this commit added, changed or removed."""


@dataclass
class FakeRepo:
    owner: str
    name: str
    default_branch: str = "main"
    can_push: bool = True
    protected: set[str] = field(default_factory=set)
    """Branches that only take changes through a pull request."""
    branches: dict[str, str] = field(default_factory=dict)
    """Branch name -> commit ID. Delete one to make the branch disappear."""
    commits: list[Commit] = field(default_factory=list)
    """Every commit that landed on a branch, oldest first."""
    blobs: dict[str, bytes] = field(default_factory=dict)
    trees: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    """Tree ID -> ``name -> (mode, ID)``."""
    commit_objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Commit ID -> ``{"tree", "parents", "message"}``, landed or not."""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    # Git objects

    def store_blob(self, data: bytes) -> str:
        sha = git_blob_sha(data)
        self.blobs[sha] = data
        return sha

    def store_tree(self, entries: Mapping[str, tuple[str, str]]) -> str:
        canonical = json.dumps(sorted(entries.items())).encode()
        sha = hashlib.sha1(b"tree " + canonical).hexdigest()
        self.trees[sha] = dict(entries)
        return sha

    def store_commit(self, tree: str, parents: Sequence[str], message: str) -> str:
        # Real commit IDs include a timestamp, so two identical commits differ.
        content = json.dumps([tree, list(parents), message, len(self.commit_objects)])
        sha = hashlib.sha1(b"commit " + content.encode()).hexdigest()
        self.commit_objects[sha] = {"tree": tree, "parents": list(parents), "message": message}
        return sha

    def flatten(self, tree: str, prefix: str = "") -> dict[str, str]:
        """Every file under ``tree``, ``path -> blob ID``."""
        found: dict[str, str] = {}
        for name, (mode, sha) in self.trees[tree].items():
            if mode == TREE_MODE:
                found |= self.flatten(sha, f"{prefix}{name}/")
            else:
                found[f"{prefix}{name}"] = sha
        return found

    def build(self, files: Mapping[str, str]) -> str:
        """A tree holding ``files`` (``path -> blob ID``); stores every subtree."""
        here: dict[str, tuple[str, str]] = {}
        below: dict[str, dict[str, str]] = {}
        for path, sha in files.items():
            name, _, rest = path.partition("/")
            if rest:
                below.setdefault(name, {})[rest] = sha
            else:
                here[name] = (FILE_MODE, sha)
        for name, children in below.items():
            if name in here:
                raise _Fail(_error(422, f"{name} is a file, not a directory"))
            here[name] = (TREE_MODE, self.build(children))
        return self.store_tree(here)

    def head_files(self, branch: str) -> dict[str, str]:
        return self.flatten(self.commit_objects[self.branches[branch]]["tree"])

    def descends(self, commit: str, ancestor: str) -> bool:
        pending, seen = [commit], set()
        while pending:
            sha = pending.pop()
            if sha == ancestor:
                return True
            if sha in seen or sha not in self.commit_objects:
                continue
            seen.add(sha)
            pending.extend(self.commit_objects[sha]["parents"])
        return False

    def land(self, branch: str, commit: str) -> None:
        """Point ``branch`` at ``commit`` and log what it changed."""
        before = self.head_files(branch) if branch in self.branches else {}
        self.branches[branch] = commit
        after = self.head_files(branch)
        changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
        message = self.commit_objects[commit]["message"]
        self.commits.append(Commit(branch, message, commit, frozenset(changed)))


@dataclass
class _Response:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


class FakeGitHub:
    """In-memory repositories behind GitHub's REST API shapes."""

    def __init__(self, token: str | None = "test-token") -> None:
        self.token = token
        """The only token accepted. ``None`` accepts any request."""
        self.repos: dict[str, FakeRepo] = {}
        self.requests: list[tuple[str, str]] = []
        """Every ``(method, path)`` served, for tests to count calls."""
        self._failures: list[tuple[str | None, _Response]] = []
        self._lock = threading.Lock()

    # Setting up state

    def add_repo(
        self,
        full_name: str,
        *,
        branches: Sequence[str] = ("main",),
        files: Mapping[str, bytes | str] | None = None,
        can_push: bool = True,
    ) -> FakeRepo:
        """Add a repository. ``files`` go on every branch, in one first commit."""
        owner, name = full_name.split("/")
        repo = FakeRepo(owner=owner, name=name, default_branch=branches[0], can_push=can_push)
        tree = repo.build(
            {path: repo.store_blob(_bytes(data)) for path, data in (files or {}).items()}
        )
        first = repo.store_commit(tree, [], "Initial commit")
        for branch in branches:
            repo.land(branch, first)
        self.repos[full_name.lower()] = repo
        return repo

    def repo(self, full_name: str) -> FakeRepo:
        return self.repos[full_name.lower()]

    def files(self, full_name: str, branch: str = "main") -> dict[str, bytes]:
        """Every file on ``branch``, ``path -> bytes``."""
        repo = self.repo(full_name)
        return {path: repo.blobs[sha] for path, sha in repo.head_files(branch).items()}

    def put_file(self, full_name: str, path: str, data: bytes | str, branch: str = "main") -> None:
        """Change a file as a person on github.com would: one commit on ``branch``."""
        with self._lock:
            repo = self.repo(full_name)
            files = repo.head_files(branch) | {path: repo.store_blob(_bytes(data))}
            self._commit_by_hand(repo, branch, files, f"Edit {path} on github.com")

    def delete_file(self, full_name: str, path: str, branch: str = "main") -> None:
        with self._lock:
            repo = self.repo(full_name)
            files = repo.head_files(branch)
            del files[path]
            self._commit_by_hand(repo, branch, files, f"Delete {path} on github.com")

    def _commit_by_hand(
        self, repo: FakeRepo, branch: str, files: Mapping[str, str], message: str
    ) -> None:
        try:
            tree = repo.build(files)
        except _Fail as failure:
            raise ValueError(failure.response.body["message"]) from None
        repo.land(branch, repo.store_commit(tree, [repo.branches[branch]], message))

    def fail_next(
        self,
        status: int,
        message: str = "",
        headers: Mapping[str, str] | None = None,
        *,
        method: str | None = None,
    ) -> None:
        """Answer the next request (the next ``method`` one, if given) with this error."""
        self._failures.append(
            (
                method,
                _Response(
                    status, {"message": message or HTTPStatus(status).phrase}, dict(headers or {})
                ),
            )
        )

    # Serving

    def handle(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes
    ) -> _Response:
        """Serve one request. ``target`` is the path plus query string."""
        with self._lock:
            parts = urlsplit(target)
            self.requests.append((method, parts.path))
            for position, (only, failure) in enumerate(self._failures):
                if only is None or only == method:
                    del self._failures[position]
                    return failure
            if self.token is not None:
                auth = {k.lower(): v for k, v in headers.items()}.get("authorization", "")
                scheme, _, token = auth.partition(" ")
                if scheme.lower() not in {"bearer", "token"} or token != self.token:
                    return _error(401, "Bad credentials")
            segments = [unquote(segment) for segment in parts.path.split("/")[1:]]
            try:
                return self._route(method, segments, body)
            except _Fail as failure:
                return failure.response

    def _route(self, method: str, segments: list[str], body: bytes) -> _Response:
        if len(segments) < 3 or segments[0] != "repos":
            raise _Fail(_error(404, "Not Found"))
        repo = self.repos.get(f"{segments[1]}/{segments[2]}".lower())
        if repo is None:
            raise _Fail(_error(404, "Not Found"))
        rest = segments[3:]
        if method == "GET" and not rest:
            return _Response(200, _repo_json(repo))
        if rest[:1] != ["git"] or len(rest) < 2:
            raise _Fail(_error(404, "Not Found"))
        kind, ids = rest[1], rest[2:]
        if method == "GET" and kind == "ref" and ids[:1] == ["heads"] and len(ids) >= 2:
            return self._get_ref(repo, "/".join(ids[1:]))
        if method == "PATCH" and kind == "refs" and ids[:1] == ["heads"] and len(ids) >= 2:
            return self._update_ref(repo, "/".join(ids[1:]), _json(body))
        if method == "GET" and len(ids) == 1:
            getter = {"commits": _get_commit, "trees": _get_tree, "blobs": _get_blob}.get(kind)
            if getter is not None:
                return getter(repo, ids[0])
        if method == "POST" and not ids:
            maker = {"commits": _make_commit, "trees": _make_tree, "blobs": _make_blob}.get(kind)
            if maker is not None:
                if not repo.can_push:
                    raise _Fail(_error(403, "Resource not accessible by personal access token"))
                return maker(repo, _json(body))
        raise _Fail(_error(404, "Not Found"))

    def _get_ref(self, repo: FakeRepo, branch: str) -> _Response:
        if branch not in repo.branches:
            raise _Fail(_error(404, "Not Found"))
        return _Response(200, _ref_json(branch, repo.branches[branch]))

    def _update_ref(self, repo: FakeRepo, branch: str, request: dict[str, Any]) -> _Response:
        if branch not in repo.branches:
            raise _Fail(_error(422, "Reference does not exist"))
        sha = request.get("sha")
        if sha not in repo.commit_objects:
            raise _Fail(_error(422, "Object does not exist"))
        if branch in repo.protected:
            raise _Fail(_error(422, f"Protected branch update failed for refs/heads/{branch}."))
        if not request.get("force") and not repo.descends(sha, repo.branches[branch]):
            raise _Fail(_error(422, "Update is not a fast forward"))
        repo.land(branch, sha)
        return _Response(200, _ref_json(branch, sha))


def _get_commit(repo: FakeRepo, sha: str) -> _Response:
    commit = repo.commit_objects.get(sha)
    if commit is None:
        raise _Fail(_error(404, "Not Found"))
    return _Response(200, _commit_json(sha, commit))


def _get_tree(repo: FakeRepo, sha: str) -> _Response:
    """One folder (not recursive)."""
    entries = repo.trees.get(sha)
    if entries is None:
        raise _Fail(_error(404, "Not Found"))
    tree = [
        {
            "path": name,
            "mode": mode,
            "type": "tree" if mode == TREE_MODE else "blob",
            "sha": child,
            **({} if mode == TREE_MODE else {"size": len(repo.blobs[child])}),
        }
        for name, (mode, child) in sorted(entries.items())
    ]
    return _Response(200, {"sha": sha, "tree": tree, "truncated": False})


def _get_blob(repo: FakeRepo, sha: str) -> _Response:
    data = repo.blobs.get(sha)
    if data is None:
        raise _Fail(_error(404, "Not Found"))
    return _Response(
        200, {"sha": sha, "size": len(data), "content": _b64(data), "encoding": "base64"}
    )


def _make_blob(repo: FakeRepo, request: dict[str, Any]) -> _Response:
    content = request.get("content")
    if not isinstance(content, str):
        raise _Fail(_error(422, 'Invalid request.\n\n"content" wasn\'t supplied.'))
    if request.get("encoding") == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except binascii.Error:
            raise _Fail(_error(422, "content is not valid Base64")) from None
    else:
        data = content.encode()
    return _Response(201, {"sha": repo.store_blob(data)})


def _make_tree(repo: FakeRepo, request: dict[str, Any]) -> _Response:
    base = request.get("base_tree")
    if base is not None and base not in repo.trees:
        raise _Fail(_error(422, "base_tree is not a valid tree oid"))
    files = repo.flatten(base) if base else {}
    entries = request.get("tree")
    if not isinstance(entries, list):
        raise _Fail(_error(422, 'Invalid request.\n\n"tree" wasn\'t supplied.'))
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str) or not path or path.startswith("/") or "//" in path:
            raise _Fail(_error(422, f"tree.path contains a malformed path component: {path!r}"))
        if entry.get("mode") != FILE_MODE or entry.get("type") != "blob":
            raise _Fail(_error(422, "The fake only takes regular files (mode 100644, blob)."))
        if "content" in entry:
            files[path] = repo.store_blob(str(entry["content"]).encode())
        elif entry.get("sha") is None:
            files.pop(path, None)
        elif entry["sha"] in repo.blobs:
            files[path] = entry["sha"]
        else:
            raise _Fail(_error(422, "tree.sha is not a valid blob"))
    sha = repo.build(files)
    return _Response(201, {"sha": sha, "tree": _get_tree(repo, sha).body["tree"]})


def _make_commit(repo: FakeRepo, request: dict[str, Any]) -> _Response:
    message, tree, parents = request.get("message"), request.get("tree"), request.get("parents")
    if not isinstance(message, str) or not message:
        raise _Fail(_error(422, 'Invalid request.\n\n"message" wasn\'t supplied.'))
    if tree not in repo.trees:
        raise _Fail(_error(422, "Tree SHA does not exist"))
    if not isinstance(parents, list) or any(p not in repo.commit_objects for p in parents):
        raise _Fail(_error(422, "Parent SHA does not exist or is not a commit object"))
    sha = repo.store_commit(tree, parents, message)
    return _Response(201, _commit_json(sha, repo.commit_objects[sha]))


class _Fail(Exception):
    def __init__(self, response: _Response) -> None:
        self.response = response


def _bytes(data: bytes | str) -> bytes:
    return data.encode() if isinstance(data, str) else data


def _json(body: bytes) -> dict[str, Any]:
    try:
        request = json.loads(body or b"{}")
    except ValueError:
        raise _Fail(_error(400, "Problems parsing JSON")) from None
    if not isinstance(request, dict):
        raise _Fail(_error(400, "Problems parsing JSON"))
    return request


def _error(status: int, message: str) -> _Response:
    return _Response(
        status, {"message": message, "documentation_url": "https://docs.github.com/rest"}
    )


def _ref_json(branch: str, sha: str) -> dict[str, Any]:
    return {"ref": f"refs/heads/{branch}", "object": {"type": "commit", "sha": sha}}


def _commit_json(sha: str, commit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sha": sha,
        "message": commit["message"],
        "tree": {"sha": commit["tree"]},
        "parents": [{"sha": parent} for parent in commit["parents"]],
    }


def _repo_json(repo: FakeRepo) -> dict[str, Any]:
    return {
        "name": repo.name,
        "full_name": repo.full_name,
        "default_branch": repo.default_branch,
        "permissions": {"admin": False, "push": repo.can_push, "pull": True},
    }


def _server(fake: FakeGitHub, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            response = fake.handle(self.command, self.path, dict(self.headers.items()), body)
            payload = json.dumps(response.body).encode() if response.body is not None else b""
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_PUT = do_POST = do_PATCH = do_DELETE = _serve

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)


@contextlib.contextmanager
def serve_fake(fake: FakeGitHub, host: str = "127.0.0.1", port: int = 0) -> Iterator[str]:
    """Serve ``fake`` in a background thread. Yields its API URL."""
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
    """Run the fake until interrupted. Settings also come from ``FAKE_GITHUB_*``."""
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=env("FAKE_GITHUB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("FAKE_GITHUB_PORT", "8081")))
    parser.add_argument("--token", default=env("FAKE_GITHUB_TOKEN", "fake-token"))
    parser.add_argument(
        "--repo",
        action="append",
        default=None,
        help="owner/name of a repository to create (repeatable; default my-lab/metadata)",
    )
    args = parser.parse_args(argv)

    fake = FakeGitHub(token=args.token)
    for full_name in args.repo or [env("FAKE_GITHUB_REPO", "my-lab/metadata")]:
        fake.add_repo(full_name)
    server = _server(fake, args.host, args.port)
    print(f"Fake GitHub API on http://{args.host}:{server.server_address[1]}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    main()
