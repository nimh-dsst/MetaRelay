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

A cassette holds no real data. Recording redacts what would let someone act
as the user (the ``akid``, ``sig``, ``expires``, the login email and auth
code, the user ID, and email addresses), then replaces every other value
that came from the account with a fake one: IDs, timestamps, the person's
name, ORCID, and IP address, share links, and the name of every notebook,
folder, and page the script did not create itself. Attachment downloads are
stored by size only. What stays is the shape of each response and what the
smoke script wrote. Still review a cassette before committing it.

To replace the values in a cassette recorded before these rules::

    python -m metarelay.testing.labarchives_cassettes <cassette>.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree
from xml.sax.saxutils import unescape

import requests

__all__ = [
    "anonymize",
    "anonymize_cassette",
    "compare_shapes",
    "load_cassette",
    "record_http",
    "xml_shapes",
]

CASSETTE_VERSION = 1
REDACTED = "REDACTED"
_SECRET_PARAMS = frozenset(
    {"akid", "sig", "expires", "password", "login_or_email", "uid", "email", "auth_code"}
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
# Elements that identify the person or their network, and the fake value each gets.
_PERSONAL = {
    "fullname": "Demo User",
    "first-name": "Demo",
    "last-name": "User",
    "last-modified-by": "Demo User",
    "last-modified-ip": "192.0.2.1",
    "orcid": "0000-0000-0000-0000",
    "sso-id": "demo",
    "suborganization": "Demo lab",
    "entry-url": "https://labarchives.example/share/entry",
    "nginx-call-start": "0.000",
}
# Elements and request parameters that hold an ID, and the kind of thing it names.
_ID_KINDS = {
    "id": "notebook",
    "nbid": "notebook",
    "tree-id": "node",
    "tree_id": "node",
    "parent-tree-id": "node",
    "parent_tree_id": "node",
    "page-tree-id": "node",
    "page_tree_id": "node",
    "pid": "node",
    "eid": "entry",
}
_MIN_REAL_ID = 16
"""LabArchives' IDs are long base64 strings. Shorter values (``0`` for the top
level, the fake server's IDs, an ID already replaced) are left alone."""
# Notebook, folder, and page names, and what a name the script didn't create becomes.
_NAMES = {"name": "Notebook", "display-text": "Item"}
_NAME_PARAM = "display_text"
"""The request parameter that names a folder or page the script creates."""
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}([T ])\d{2}([:_.])\d{2}\2\d{2}(Z?)")


def _element(tags: Mapping[str, str] | Sequence[str]) -> re.Pattern[str]:
    """Matches an element with one of ``tags`` and some text: tag, attributes, text."""
    return re.compile(rf"<({'|'.join(map(re.escape, tags))})(\s[^>]*)?>([^<]+)</\1>")


_PERSONAL_ELEMENT = _element(_PERSONAL)
_ID_ELEMENT = _element([tag for tag in _ID_KINDS if "_" not in tag])
_NAME_ELEMENT = _element(_NAMES)


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


def _fake_time(match: re.Match[str]) -> str:
    separator = match[2]
    return f"2026-01-01{match[1]}00{separator}00{separator}00{match[3]}"


def anonymize(interactions: list[dict[str, Any]]) -> None:
    """Replace every value that came from the account with a fake one, in place.

    The same ID or name gets the same fake value everywhere, so a cassette
    still reads as one run. Doing it twice changes nothing more.
    """
    # 1. IDs, numbered by kind in the order they first appear. An ID also
    #    appears inside captions and file names, so replace it in every string.
    ids: dict[str, str] = {}
    counts: dict[str, int] = {}

    def see(key: str, value: str) -> None:
        if len(value) >= _MIN_REAL_ID and value not in ids and value != REDACTED:
            kind = _ID_KINDS[key]
            counts[kind] = counts.get(kind, 0) + 1
            ids[value] = f"{kind}-{counts[kind]}"

    for interaction in interactions:
        for key, value in interaction["params"].items():
            if key in _ID_KINDS:
                see(key, value)
        for match in _ID_ELEMENT.finditer(interaction["body"]):
            see(match[1], unescape(match[3]))

    def everywhere(text: str) -> str:
        for real in sorted(ids, key=len, reverse=True):
            text = text.replace(real, ids[real])
        return _TIMESTAMP.sub(_fake_time, text)

    for interaction in interactions:
        interaction["params"] = {k: everywhere(v) for k, v in interaction["params"].items()}
        interaction["body"] = everywhere(interaction["body"])
        interaction["content_disposition"] = everywhere(interaction["content_disposition"])

    # 2. The person, and the names the script did not create.
    own_names = {i["params"][_NAME_PARAM] for i in interactions if _NAME_PARAM in i["params"]}
    names: dict[tuple[str, str], str] = {}

    def name(match: re.Match[str]) -> str:
        tag, text = match[1], unescape(match[3])
        label = _NAMES[tag]
        if text in own_names or text == REDACTED or re.fullmatch(rf"{label} \d+", text):
            return match[0]
        fake = names.setdefault((tag, text), f"{label} {len(names) + 1}")
        return f"<{tag}{match[2] or ''}>{fake}</{tag}>"

    for interaction in interactions:
        body = _PERSONAL_ELEMENT.sub(
            lambda m: f"<{m[1]}{m[2] or ''}>{_PERSONAL[m[1]]}</{m[1]}>", interaction["body"]
        )
        interaction["body"] = _NAME_ELEMENT.sub(name, body)


def _interaction(response: requests.Response) -> dict[str, Any]:
    """One call. The body is not redacted yet: ``record_http`` does that at the end."""
    request = response.request
    content_type = response.headers.get("Content-Type", "")
    is_xml = "xml" in content_type or response.content.lstrip().startswith(b"<")
    if is_xml and "entry_attachment" not in (request.url or ""):
        body = response.content.decode(errors="replace")
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
        interactions.append(_interaction(response))
        return response

    requests.Session.send = send  # type: ignore[method-assign]
    try:
        yield interactions
    finally:
        requests.Session.send = original  # type: ignore[method-assign]
        # At the end: a secret learned late may appear in an earlier body,
        # and a folder may be listed before the call that names it.
        _write(path, interactions, secrets)


def _write(path: str | Path, interactions: list[dict[str, Any]], secrets: set[str]) -> None:
    for interaction in interactions:
        interaction["body"] = _redact_text(interaction["body"], secrets)
    anonymize(interactions)
    document = {"version": CASSETTE_VERSION, "interactions": interactions}
    Path(path).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def anonymize_cassette(path: str | Path) -> None:
    """Replace the real values in the cassette at ``path`` with fake ones, in place.

    For a cassette recorded before a rule was added. It can't find a secret
    the recording missed by value (the user ID, say), only by element.
    """
    _write(path, [dict(interaction) for interaction in load_cassette(path)], set())


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
            # At any depth, as labapi reads it: LabArchives nests the error
            # inside the API class's root (<tree-tools><error><error-code>).
            code = ElementTree.fromstring(interaction["body"].encode()).findtext(".//error-code")
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m metarelay.testing.labarchives_cassettes",
        description="Replace the real values in a recorded cassette with fake ones, in place.",
    )
    parser.add_argument("cassette", type=Path, nargs="+", help="a cassette file")
    for path in parser.parse_args(argv).cassette:
        anonymize_cassette(path)
        print(f"Anonymized {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
