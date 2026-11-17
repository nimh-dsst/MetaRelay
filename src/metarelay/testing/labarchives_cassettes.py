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
stored by size only. What stays is the shape of each response, LabArchives'
own fixed values, and what the smoke script wrote.

This fails closed. Every element, attribute, and request parameter has to be
on a list below that says what it holds. ``leaks`` reports anything that is
not, and any listed value that is still real, and recording refuses to write
a cassette that has one. When LabArchives sends something new, add it to the
right list. Still review a cassette before committing it.

To replace the values in a cassette recorded before these rules::

    python -m metarelay.testing.labarchives_cassettes <cassette>.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree
from xml.sax.saxutils import unescape

import requests

__all__ = [
    "CassetteLeakError",
    "anonymize",
    "anonymize_cassette",
    "compare_shapes",
    "leaks",
    "load_cassette",
    "record_http",
    "xml_shapes",
]

CASSETTE_VERSION = 1
REDACTED = "REDACTED"


class CassetteLeakError(ValueError):
    """A cassette was not written, because it could hold real data."""


_FAKE_EMAIL = "user@example.org"
_FAKE_TIME = "2026-01-01T00:00:00Z"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")

# What each request parameter holds. A parameter on no list is a leak.
_SECRET_PARAMS = frozenset(
    {"akid", "sig", "expires", "password", "login_or_email", "uid", "email", "auth_code"}
)
_NAME_PARAM = "display_text"
"""Names a folder or page the script creates."""
_WRITTEN_PARAMS = frozenset(
    {"caption", "change_description", "filename", "entry_data", "is_folder", "part_type"}
)
"""What the script wrote, and LabArchives' own fixed values."""

# What each element holds. An element with text that is on no list is a leak.
_SECRET = frozenset({"akid", "sig", "expires", "uid", "password", "login-or-email", "email"})
"""Always redacted, also when the value was not seen as a request parameter."""
# Elements that identify the person, their network, or the time, and the fake value each gets.
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
    "created-at": _FAKE_TIME,
    "updated-at": _FAKE_TIME,
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
_FIXED_IDS = frozenset({"0", "999999999", REDACTED})
"""IDs that name nothing real: the top level, the smoke script's missing folder."""
_FAKE_ID = re.compile(r"(?:notebook|node|entry)-\d+")
_MIN_ID_IN_TEXT = 16
"""An ID is always replaced where it is an element's or a parameter's whole
value. It also appears inside captions and file names. There it is replaced
only when it is this long, as LabArchives' base64 IDs are: a short ID could
be part of an unrelated word."""
# Notebook, folder, and page names, and what a name the script didn't create becomes.
_NAMES = {"name": "Notebook", "display-text": "Item"}
_FIXED = frozenset(
    {
        "attach-content-type",
        "attach-file-size",
        "can-own-notebooks",
        "can-read",
        "can-read-comments",
        "can-write",
        "can-write-comments",
        "class",
        "error-code",
        "flags",
        "is-a-researcher",
        "is-a-student",
        "is-a-teacher",
        "is-default",
        "is-folder",
        "is-page",
        "is-page-locked",
        "max-file-size",
        "method",
        "number-comments",
        "part-type",
        "snapshot",
        "thumb-info",
        "total-returned",
        "version",
    }
)
"""LabArchives' own values: flags, counts, sizes, and fixed words."""
# Sentences LabArchives writes. Only the wording seen so far is trusted: a new
# one could repeat something from the request, such as a notebook's name.
_KNOWN_TEXT = {
    "error-description": frozenset({"your request contained bad information"}),
    "last-modified-verb": frozenset({"file uploaded or modified by Partner application"}),
}
_WRITTEN = frozenset(
    {"attach-file-name", "caption", "change-description", "entry-data", "filename"}
)
"""What the smoke script wrote. It only reads entries on pages it created."""
_ATTRIBUTES = frozenset({"type", "nil"})
"""The attributes LabArchives puts on elements: a data type, or that there is no value."""

# A timestamp, with any fraction of a second and any timezone.
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}([T ])\d{2}([:_.])\d{2}\2\d{2}(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?"
)
_IP_ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_LINK = re.compile(r"https?://[^/\s\"<]+")
_SIZE_ONLY = re.compile(r"<\d+ bytes>")


def _element(tags: Mapping[str, str] | Sequence[str]) -> re.Pattern[str]:
    """Matches an element with one of ``tags`` and some text: tag, attributes, text."""
    return re.compile(rf"<({'|'.join(map(re.escape, tags))})(\s[^>]*)?>([^<]+)</\1>")


_SECRET_ELEMENT = _element(sorted(_SECRET))
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
    zone = "Z" if match[3] else ""  # never the recorder's own offset
    return f"2026-01-01{match[1]}00{separator}00{separator}00{zone}"


def _is_fake_id(value: str) -> bool:
    return value in _FIXED_IDS or _FAKE_ID.fullmatch(value) is not None


def _is_fake_name(tag: str, text: str) -> bool:
    return text == REDACTED or re.fullmatch(rf"{_NAMES[tag]} \d+", text) is not None


def _own_names(interactions: Sequence[Mapping[str, Any]]) -> set[str]:
    return {i["params"][_NAME_PARAM] for i in interactions if _NAME_PARAM in i["params"]}


def _next_free(label: str, taken: set[str]) -> str:
    """The first ``label`` + number that nothing in the cassette is called yet."""
    number = 1
    while f"{label}{number}" in taken:
        number += 1
    taken.add(f"{label}{number}")
    return f"{label}{number}"


def anonymize(interactions: list[dict[str, Any]]) -> None:
    """Replace every value that came from the account with a fake one, in place.

    The same ID or name gets the same fake value everywhere, so a cassette
    still reads as one run. Doing it twice changes nothing more. It handles
    only what the lists above name: check the result with ``leaks``.
    """

    def swap(pattern: re.Pattern[str], value: Any, text: str) -> str:
        """Give each element ``pattern`` matches the text ``value`` returns for it."""
        return pattern.sub(lambda m: f"<{m[1]}{m[2] or ''}>{value(m)}</{m[1]}>", text)

    # 1. IDs, numbered by kind in the order they first appear, skipping any
    #    number a fake ID in the cassette already has.
    ids: dict[str, str] = {}
    taken = {
        match
        for interaction in interactions
        for text in (*interaction["params"].values(), interaction["body"])
        for match in _FAKE_ID.findall(text)
    }

    def see(key: str, value: str) -> None:
        if value not in ids and not _is_fake_id(value):
            ids[value] = _next_free(f"{_ID_KINDS[key]}-", taken)

    for interaction in interactions:
        for key, value in interaction["params"].items():
            if key in _ID_KINDS:
                see(key, value)
        for match in _ID_ELEMENT.finditer(interaction["body"]):
            see(match[1], unescape(match[3]))
    in_text = sorted((i for i in ids if len(i) >= _MIN_ID_IN_TEXT), key=len, reverse=True)

    def everywhere(text: str) -> str:
        for real in in_text:
            text = text.replace(real, ids[real])
        return _TIMESTAMP.sub(_fake_time, text)

    for interaction in interactions:
        interaction["params"] = {
            key: ids.get(value, value) if key in _ID_KINDS else everywhere(value)
            for key, value in interaction["params"].items()
        }
        body = swap(_ID_ELEMENT, lambda m: ids.get(unescape(m[3]), m[3]), interaction["body"])
        interaction["body"] = everywhere(body)
        interaction["content_disposition"] = everywhere(interaction["content_disposition"])

    # 2. Secrets, the person, and the names the script did not create.
    own_names = _own_names(interactions)
    names: dict[tuple[str, str], str] = {}
    kept = {
        unescape(match[3])
        for interaction in interactions
        for match in _NAME_ELEMENT.finditer(interaction["body"])
    }

    def name(match: re.Match[str]) -> str:
        tag, text = match[1], unescape(match[3])
        if text in own_names or _is_fake_name(tag, text):
            return match[3]
        if (tag, text) not in names:
            names[tag, text] = _next_free(f"{_NAMES[tag]} ", kept)
        return names[tag, text]

    for interaction in interactions:
        body = swap(_SECRET_ELEMENT, lambda m: REDACTED, interaction["body"])
        body = swap(_PERSONAL_ELEMENT, lambda m: _PERSONAL[m[1]], body)
        interaction["body"] = _EMAIL.sub(_FAKE_EMAIL, swap(_NAME_ELEMENT, name, body))


def leaks(interactions: Sequence[Mapping[str, Any]]) -> list[str]:
    """Everything in a cassette that is, or could be, real data. Empty when there is none.

    It reports a request parameter, element, or attribute that no list above
    covers, and a listed value that is not the fake one. So a response field
    LabArchives adds later is reported until someone says what it holds.
    """
    own_names = _own_names(interactions)
    found: dict[str, None] = {}  # in order, without repeats

    def leak(interaction: Mapping[str, Any], what: str) -> None:
        found[f"{interaction['api']}: {what}"] = None

    def check_text(interaction: Mapping[str, Any], text: str) -> None:
        for match in _TIMESTAMP.finditer(text):
            if _fake_time(match) != match[0]:
                leak(interaction, f"a real timestamp ({match[0]})")
        for address in _IP_ADDRESS.findall(text):
            if address != _PERSONAL["last-modified-ip"]:
                leak(interaction, f"an IP address ({address})")
        for link in _LINK.findall(text):
            if link != "https://labarchives.example":
                leak(interaction, f"a link ({link})")
        for match in _EMAIL.finditer(text):
            if match[0] != _FAKE_EMAIL:
                leak(interaction, "an email address")

    def check_element(interaction: Mapping[str, Any], element: ElementTree.Element) -> None:
        tag, text = element.tag, (element.text or "").strip()
        for attribute in element.attrib:
            if attribute not in _ATTRIBUTES:
                leak(interaction, f"the attribute {attribute!r} on <{tag}> is not on a list")
        if (element.tail or "").strip():
            leak(interaction, f"there is text after <{tag}>")
        if not text or tag in _FIXED or tag in _WRITTEN:
            return
        if tag in _SECRET:
            fake = text in (REDACTED, _FAKE_EMAIL)
        elif tag in _PERSONAL:
            fake = text == _PERSONAL[tag]
        elif tag in _ID_KINDS:
            fake = _is_fake_id(text)
        elif tag in _NAMES:
            fake = text in own_names or _is_fake_name(tag, text)
        elif tag in _KNOWN_TEXT:
            if text not in _KNOWN_TEXT[tag]:
                leak(interaction, f"<{tag}> holds wording that is not on its list")
            return
        else:
            leak(interaction, f"the element <{tag}> is not on a list")
            return
        if not fake:
            leak(interaction, f"<{tag}> holds a real value")

    for interaction in interactions:
        for key, value in interaction["params"].items():
            if key in _SECRET_PARAMS:
                fake = value in ("", REDACTED)
            elif key in _ID_KINDS:
                fake = _is_fake_id(value)
            elif key == _NAME_PARAM or key in _WRITTEN_PARAMS:
                fake = True
            else:
                leak(interaction, f"the parameter {key!r} is not on a list")
                continue
            if not fake:
                leak(interaction, f"the parameter {key!r} holds a real value")
            check_text(interaction, value)
        check_text(interaction, interaction["content_disposition"])
        body = interaction["body"]
        check_text(interaction, body)
        if not body or _SIZE_ONLY.fullmatch(body):
            continue
        try:
            root = ElementTree.fromstring(body.encode())
        except ElementTree.ParseError:
            leak(interaction, "the response is not XML, so it can't be checked")
            continue
        for element in root.iter():
            check_element(interaction, element)
    return list(found)


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
    """Record every ``requests`` call made inside the block to a cassette at ``path``.

    Raises:
        CassetteLeakError: If the cassette could hold real data, so it was
            not written. When the block itself raised, that error is the one
            raised, with the leak attached as a note: the block's failure is
            the first thing to fix, and often the cause of the leak.
    """
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
    # The cassette is written at the end: a secret learned late may appear in
    # an earlier body, and a folder may be listed before the call that names it.
    try:
        yield interactions
    except BaseException as failure:
        requests.Session.send = original  # type: ignore[method-assign]
        try:
            _write(path, interactions, secrets)
        except CassetteLeakError as leak:
            failure.add_note(str(leak))
        raise
    else:
        requests.Session.send = original  # type: ignore[method-assign]
        _write(path, interactions, secrets)


def _write(path: str | Path, interactions: list[dict[str, Any]], secrets: set[str]) -> None:
    for interaction in interactions:
        interaction["body"] = _redact_text(interaction["body"], secrets)
    anonymize(interactions)
    problems = leaks(interactions)
    if problems:
        raise CassetteLeakError(
            f"Not writing {path}: it could hold real data. Say what each of these holds "
            "in metarelay/testing/labarchives_cassettes.py, then try again:\n  "
            + "\n  ".join(problems)
        )
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
        try:
            anonymize_cassette(path)
        except CassetteLeakError as error:
            print(error, file=sys.stderr)
            return 1
        print(f"Anonymized {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
