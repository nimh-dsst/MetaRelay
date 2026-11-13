"""A stateful fake of the GitHub REST API, for tests and the dev stack.

It answers the calls the ``github://`` backend makes, with GitHub's status
codes and JSON shapes, so the real ``httpx`` client runs against it
unchanged: point ``GitHubConfig(api_url=...)`` (or
``METARELAY_GITHUB_API_URL``) at it.

What it does:

- keeps repositories in memory, each with branches of ``path -> bytes``;
  folders exist only because files are in them, as in Git;
- checks the bearer token on every call (401 ``Bad credentials`` otherwise);
- answers ``GET /repos/{owner}/{repo}``, ``GET .../branches/{branch}``,
  ``GET .../contents/{path}?ref=`` (a file or a folder listing),
  ``GET .../git/blobs/{sha}`` and ``PUT .../contents/{path}``;
- enforces the blob ``sha`` the way GitHub does: a ``PUT`` to an existing
  file without a ``sha`` is 422, and one with a stale ``sha`` is 409;
- uses Git's real blob SHA-1, so ``sha`` values match what GitHub would
  report for the same bytes.

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
requests, to test error handling.

The fake implements only what the backend uses. Where it and GitHub
disagree, the fake is wrong.

Standard library only, so it runs without ``httpx`` installed.
"""

from __future__ import annotations

import argparse
import base64
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
from urllib.parse import parse_qsl, unquote, urlsplit

__all__ = ["FakeGitHub", "FakeRepo", "git_blob_sha", "serve_fake"]

INLINE_LIMIT = 1024 * 1024
"""GitHub's contents API inlines files up to 1 MB; larger ones come back
with ``encoding: none`` and must be read as blobs."""


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
    path: str
    message: str
    sha: str


@dataclass
class FakeRepo:
    owner: str
    name: str
    default_branch: str = "main"
    can_push: bool = True
    protected: set[str] = field(default_factory=set)
    """Branches that only take changes through a pull request (a 409, as GitHub sends)."""
    branches: dict[str, dict[str, bytes]] = field(default_factory=dict)
    commits: list[Commit] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


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
        self._failures: list[_Response] = []
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
        """Add a repository. ``files`` go on every branch."""
        owner, name = full_name.split("/")
        repo = FakeRepo(owner=owner, name=name, default_branch=branches[0], can_push=can_push)
        content = {path: _bytes(data) for path, data in (files or {}).items()}
        for branch in branches:
            repo.branches[branch] = dict(content)
        self.repos[full_name.lower()] = repo
        return repo

    def repo(self, full_name: str) -> FakeRepo:
        return self.repos[full_name.lower()]

    def files(self, full_name: str, branch: str = "main") -> dict[str, bytes]:
        """Every file on ``branch``, ``path -> bytes``."""
        return dict(self.repo(full_name).branches[branch])

    def put_file(self, full_name: str, path: str, data: bytes | str, branch: str = "main") -> None:
        """Change a file as a person on github.com would, behind the client's back."""
        with self._lock:
            repo = self.repo(full_name)
            repo.branches[branch][path] = _bytes(data)
            repo.commits.append(Commit(branch, path, "Edit on github.com", _commit_sha(repo)))

    def delete_file(self, full_name: str, path: str, branch: str = "main") -> None:
        with self._lock:
            del self.repo(full_name).branches[branch][path]

    def fail_next(
        self, status: int, message: str = "", headers: Mapping[str, str] | None = None
    ) -> None:
        """Answer the next request with this error instead of serving it."""
        self._failures.append(
            _Response(
                status, {"message": message or HTTPStatus(status).phrase}, dict(headers or {})
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
            if self._failures:
                return self._failures.pop(0)
            if self.token is not None:
                auth = {k.lower(): v for k, v in headers.items()}.get("authorization", "")
                scheme, _, token = auth.partition(" ")
                if scheme.lower() not in {"bearer", "token"} or token != self.token:
                    return _error(401, "Bad credentials")
            segments = [unquote(segment) for segment in parts.path.split("/")[1:]]
            query = dict(parse_qsl(parts.query))
            try:
                return self._route(method, segments, query, body)
            except _Fail as failure:
                return failure.response

    def _route(
        self, method: str, segments: list[str], query: dict[str, str], body: bytes
    ) -> _Response:
        if len(segments) < 3 or segments[0] != "repos":
            raise _Fail(_error(404, "Not Found"))
        repo = self.repos.get(f"{segments[1]}/{segments[2]}".lower())
        if repo is None:
            raise _Fail(_error(404, "Not Found"))
        rest = segments[3:]
        if method == "GET" and not rest:
            return _Response(200, _repo_json(repo))
        if method == "GET" and rest[:1] == ["branches"] and len(rest) >= 2:
            branch = "/".join(rest[1:])
            if branch not in repo.branches:
                raise _Fail(_error(404, "Branch not found"))
            return _Response(200, {"name": branch, "commit": {"sha": _commit_sha(repo)}})
        if method == "GET" and rest[:2] == ["git", "blobs"] and len(rest) == 3:
            return self._get_blob(repo, rest[2])
        if rest[:1] == ["contents"]:
            path = "/".join(part for part in rest[1:] if part)
            if method == "GET":
                return self._get_contents(repo, path, query.get("ref") or repo.default_branch)
            if method == "PUT":
                return self._put_contents(repo, path, body)
        raise _Fail(_error(404, "Not Found"))

    def _branch(self, repo: FakeRepo, ref: str) -> dict[str, bytes]:
        if ref not in repo.branches:
            raise _Fail(_error(404, f"No commit found for the ref {ref}"))
        return repo.branches[ref]

    def _get_contents(self, repo: FakeRepo, path: str, ref: str) -> _Response:
        files = self._branch(repo, ref)
        if path in files:
            data = files[path]
            entry = _entry(path, "file", data)
            if len(data) > INLINE_LIMIT:
                entry |= {"content": "", "encoding": "none"}
            else:
                entry |= {"content": _b64(data), "encoding": "base64"}
            return _Response(200, entry)
        prefix = f"{path}/" if path else ""
        children: dict[str, dict[str, Any]] = {}
        for file_path, data in files.items():
            if not file_path.startswith(prefix):
                continue
            name, _, deeper = file_path[len(prefix) :].partition("/")
            child = f"{prefix}{name}"
            if deeper:
                children.setdefault(name, _entry(child, "dir", child.encode()))
            else:
                children[name] = _entry(child, "file", data)
        if not children:
            raise _Fail(_error(404, "Not Found"))
        return _Response(200, [children[name] for name in sorted(children)])

    def _get_blob(self, repo: FakeRepo, sha: str) -> _Response:
        for files in repo.branches.values():
            for data in files.values():
                if git_blob_sha(data) == sha:
                    return _Response(
                        200,
                        {
                            "sha": sha,
                            "size": len(data),
                            "content": _b64(data),
                            "encoding": "base64",
                        },
                    )
        raise _Fail(_error(404, "Not Found"))

    def _put_contents(self, repo: FakeRepo, path: str, body: bytes) -> _Response:
        try:
            request = json.loads(body or b"{}")
        except ValueError:
            raise _Fail(_error(400, "Problems parsing JSON")) from None
        if not path:
            raise _Fail(
                _error(422, "Invalid request.\n\nFor 'properties/path', nil is not a string.")
            )
        if not isinstance(request.get("message"), str) or not request["message"]:
            raise _Fail(_error(422, 'Invalid request.\n\n"message" wasn\'t supplied.'))
        try:
            data = base64.b64decode(request.get("content", ""), validate=True)
        except ValueError:
            raise _Fail(_error(422, "content is not valid Base64")) from None
        branch = request.get("branch") or repo.default_branch
        if branch not in repo.branches:
            raise _Fail(_error(404, f"Branch {branch} not found"))
        if branch in repo.protected:
            raise _Fail(_error(409, "Changes must be made through a pull request."))
        if not repo.can_push:
            raise _Fail(_error(403, "Resource not accessible by personal access token"))
        files = repo.branches[branch]
        parents = path.split("/")[:-1]
        for depth in range(1, len(parents) + 1):
            if "/".join(parents[:depth]) in files:
                raise _Fail(_error(422, f"{'/'.join(parents[:depth])} is a file, not a directory"))
        if any(other.startswith(f"{path}/") for other in files):
            raise _Fail(_error(422, f"{path} is a directory"))
        sha = request.get("sha")
        if path in files:
            if not sha:
                raise _Fail(_error(422, 'Invalid request.\n\n"sha" wasn\'t supplied.'))
            if sha != git_blob_sha(files[path]):
                raise _Fail(_error(409, f"{path} does not match {sha}"))
            status = 200
        else:
            if sha:
                raise _Fail(_error(409, f"{path} does not match {sha}"))
            status = 201
        files[path] = data
        commit = Commit(branch, path, request["message"], _commit_sha(repo))
        repo.commits.append(commit)
        return _Response(
            status, {"content": _entry(path, "file", data), "commit": {"sha": commit.sha}}
        )


class _Fail(Exception):
    def __init__(self, response: _Response) -> None:
        self.response = response


def _bytes(data: bytes | str) -> bytes:
    return data.encode() if isinstance(data, str) else data


def _error(status: int, message: str) -> _Response:
    return _Response(
        status, {"message": message, "documentation_url": "https://docs.github.com/rest"}
    )


def _entry(path: str, kind: str, data: bytes) -> dict[str, Any]:
    return {
        "type": kind,
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "sha": git_blob_sha(data) if kind == "file" else hashlib.sha1(data).hexdigest(),
        "size": len(data) if kind == "file" else 0,
    }


def _repo_json(repo: FakeRepo) -> dict[str, Any]:
    return {
        "name": repo.name,
        "full_name": repo.full_name,
        "default_branch": repo.default_branch,
        "permissions": {"admin": False, "push": repo.can_push, "pull": True},
    }


def _commit_sha(repo: FakeRepo) -> str:
    return hashlib.sha1(f"{repo.full_name}:{len(repo.commits)}".encode()).hexdigest()


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

        do_GET = do_PUT = do_POST = do_DELETE = _serve

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
