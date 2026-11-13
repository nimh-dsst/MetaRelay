"""Cassettes: recorded LabArchives traffic, for checking the fake against the real thing.

The fake LabArchives server (``labarchives_fake``) is built from reading
``labapi``, not from watching LabArchives. Cassettes close that gap (R1):

1. DSST runs the smoke script against real LabArchives with ``--record``,
   which saves every request and response to a JSON cassette.
2. The cassette goes in ``tests/cassettes/labarchives/``.
3. ``test_fake_matches_captured_cassettes`` runs the same script against the
   fake and compares the XML **shape** of each call's responses (which
   elements appear, not their values). Any element the fake sends that
   LabArchives doesn't is a bug in the fake.

Recording redacts what would let someone act as the user: the ``akid``,
``sig``, ``expires``, the login email and auth code, the user ID, and email
addresses in response bodies. Attachment downloads are stored by size only.
Responses still contain notebook, folder, and entry names from the test
notebook, so review a cassette before committing it.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree

import requests

__all__ = ["compare_shapes", "load_cassette", "record_http", "xml_shapes"]

CASSETTE_VERSION = 1
REDACTED = "REDACTED"
_SECRET_PARAMS = frozenset(
    {"akid", "sig", "expires", "password", "login_or_email", "uid", "email", "auth_code"}
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def _api_name(url: str) -> str:
    path = urlsplit(url).path
    if path.rstrip("/").endswith("api_user_login"):
        return "api_user_login"
    return path.split("/api/", 1)[-1].strip("/")


def _redact_params(url: str) -> dict[str, str]:
    return {
        name: REDACTED if name in _SECRET_PARAMS else value
        for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
    }


def _redact_text(text: str, secrets: set[str]) -> str:
    # Only whole element text: replacing a short user ID everywhere would
    # also change any longer ID that happens to contain it.
    for secret in secrets:
        text = text.replace(f">{secret}<", f">{REDACTED}<")
    return _EMAIL.sub("user@example.org", text)


def _interaction(response: requests.Response, secrets: set[str]) -> dict[str, Any]:
    request = response.request
    content_type = response.headers.get("Content-Type", "")
    is_xml = "xml" in content_type or response.content.lstrip().startswith(b"<")
    if is_xml and "entry_attachment" not in (request.url or ""):
        body = _redact_text(response.content.decode(errors="replace"), secrets)
    else:
        body = f"<{len(response.content)} bytes>"
    return {
        "method": request.method,
        "api": _api_name(request.url or ""),
        "params": _redact_params(request.url or ""),
        "status": response.status_code,
        "content_type": content_type,
        "content_disposition": response.headers.get("Content-Disposition", ""),
        "body": body,
    }


@contextlib.contextmanager
def record_http(path: str | Path) -> Iterator[list[dict[str, Any]]]:
    """Record every ``requests`` call made inside the block to a cassette at ``path``."""
    interactions: list[dict[str, Any]] = []
    secrets: set[str] = set()
    original = requests.Session.send

    def send(session: requests.Session, request: requests.PreparedRequest, **kwargs: Any):
        response = original(session, request, **kwargs)
        for name, value in parse_qsl(urlsplit(request.url or "").query):
            if name in _SECRET_PARAMS and value:
                secrets.add(value)
        # The login response holds the user ID, sent as uid on later calls.
        if _api_name(request.url or "") == "users/user_access_info" and response.ok:
            with contextlib.suppress(ElementTree.ParseError):
                uid = ElementTree.fromstring(response.content).findtext("./id")
                if uid:
                    secrets.add(uid)
        interactions.append(_interaction(response, secrets))
        return response

    requests.Session.send = send  # type: ignore[method-assign]
    try:
        yield interactions
    finally:
        requests.Session.send = original  # type: ignore[method-assign]
        # Redact again: a secret learned late may appear in an earlier body.
        for interaction in interactions:
            interaction["body"] = _redact_text(interaction["body"], secrets)
        document = {"version": CASSETTE_VERSION, "interactions": interactions}
        Path(path).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def load_cassette(path: str | Path) -> list[dict[str, Any]]:
    """The interactions in a cassette file."""
    document = json.loads(Path(path).read_text())
    if document.get("version") != CASSETTE_VERSION:
        raise ValueError(f"{path} is not a version {CASSETTE_VERSION} cassette.")
    return list(document["interactions"])


def _paths(element: ElementTree.Element, prefix: str = "") -> Iterator[str]:
    for child in element:
        path = f"{prefix}{child.tag}"
        yield path
        yield from _paths(child, f"{path}/")


def xml_shapes(interactions: list[Mapping[str, Any]]) -> dict[str, set[str]]:
    """For each API call, every element path seen in its successful responses.

    Paths are relative to the root element, because ``labapi`` never reads
    the root's tag. Error responses and attachment downloads are skipped.
    """
    shapes: dict[str, set[str]] = {}
    for interaction in interactions:
        if interaction["status"] != 200 or not interaction["body"].lstrip().startswith("<"):
            continue
        try:
            root = ElementTree.fromstring(interaction["body"].encode())
        except ElementTree.ParseError:
            continue
        shapes.setdefault(interaction["api"], set()).update(_paths(root))
    return shapes


def error_codes(interactions: list[Mapping[str, Any]]) -> dict[str, set[str]]:
    """For each API call, the error codes seen in its error responses."""
    codes: dict[str, set[str]] = {}
    for interaction in interactions:
        if interaction["status"] == 200 or not interaction["body"].lstrip().startswith("<"):
            continue
        try:
            code = ElementTree.fromstring(interaction["body"].encode()).findtext("error-code")
        except ElementTree.ParseError:
            continue
        if code:
            codes.setdefault(interaction["api"], set()).add(code.strip())
    return codes


def compare_error_codes(*, live: Mapping[str, set[str]], fake: Mapping[str, set[str]]) -> list[str]:
    """Calls that failed in both runs, but with different error codes.

    The smoke script asks for a missing folder on purpose, so this checks the
    fake's not-found code against LabArchives' real one.
    """
    return [
        f"{api}: LabArchives answered error {', '.join(sorted(live[api]))}; "
        f"the fake answered {', '.join(sorted(fake[api]))}."
        for api in sorted(live.keys() & fake.keys())
        if live[api] != fake[api]
    ]


def compare_shapes(*, live: Mapping[str, set[str]], fake: Mapping[str, set[str]]) -> list[str]:
    """Where the fake's responses differ in shape from LabArchives'.

    Lines ending "LabArchives does not." mean the fake sends an element
    LabArchives doesn't, and are bugs in the fake. Elements only LabArchives sends are
    listed too: ``labapi`` ignores them today, but the fake may want them.
    """
    differences = []
    for api in sorted(live.keys() | fake.keys()):
        if api not in fake:
            differences.append(f"{api}: LabArchives was called but the fake was not.")
            continue
        if api not in live:
            differences.append(f"{api}: the fake was called but LabArchives was not.")
            continue
        for path in sorted(live[api] - fake[api]):
            differences.append(f"{api}: LabArchives sends {path}; the fake does not.")
        for path in sorted(fake[api] - live[api]):
            differences.append(f"{api}: the fake sends {path}; LabArchives does not.")
    return differences
