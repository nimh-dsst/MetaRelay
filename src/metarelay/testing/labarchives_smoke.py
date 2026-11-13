"""A live smoke test for the LabArchives backend, for DSST to run with real credentials (R1).

Run it against real LabArchives::

    export METARELAY_LABARCHIVES_AKID=...      # the institution's API key pair
    export METARELAY_LABARCHIVES_AKPASS=...
    python -m metarelay.testing.labarchives_smoke --notebook "My test notebook" \\
        --record labarchives-live.json

It prints a login link. Log in, then paste the address the browser was sent
back to (the page itself may fail to load; only its address matters).

It writes into a new folder, ``MetaRelay smoke tests/run <time>``, in the
notebook you name, and checks write, read, update, conflict detection, file
upload, and a page used as a container. ``labapi`` cannot delete anything, so
remove the folder in LabArchives afterwards. Use a test notebook.

It also reports whether LabArchives keeps the hidden marker (an HTML comment)
MetaRelay puts in each preview entry. MetaRelay finds previews through the
attachment's caption, so either answer works; the marker is only the
fallback after a write that failed halfway. And it asks for a folder that
doesn't exist, so the cassette records LabArchives' real not-found error
code. ``--check-edits`` adds a step where you edit the preview in
LabArchives by hand, to check that the next write refuses to overwrite it.

``--record`` saves the traffic as a cassette (secrets redacted; see
``labarchives_cassettes``). Review it, then add it to
``tests/cassettes/labarchives/`` so the fake can be checked against it.

CI runs the same script against the fake server with ``--auto-login``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import labapi
import pydantic
import requests

from metarelay.backends.labarchives import (
    RECORDS_PAGE,
    LabArchivesSession,
    _labarchives_errors,
    _marker,
)
from metarelay.core import registry as registry_module
from metarelay.core.backend import begin_login, complete_login
from metarelay.core.errors import ConflictError, MetaRelayError, ValidationError
from metarelay.core.path import ROOT, LogicalPath
from metarelay.core.record import Record
from metarelay.core.session import Session
from metarelay.testing.labarchives_cassettes import record_http

__all__ = ["SMOKE_SCHEMA_ID", "main", "run_smoke"]

SMOKE_SCHEMA_ID = "metarelay.smoke"
SMOKE_FOLDER = "MetaRelay smoke tests"
SMOKE_PAGE = "Subject page"
MISSING_TREE_ID = "999999999"


class SmokePayload(pydantic.BaseModel):
    """The payload of the record the smoke test writes."""

    model_config = pydantic.ConfigDict(extra="forbid")

    label: str
    value: float


class SmokeFailure(Exception):
    """A smoke check did not hold."""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _record(label: str, value: float) -> Record:
    return Record(
        schema_id=SMOKE_SCHEMA_ID,
        schema_version=1,
        record_id="smoke-1",
        context={"purpose": "MetaRelay smoke test"},
        payload={"label": label, "value": value},
    )


def run_smoke(
    session: Session,
    notebook: str,
    say: Callable[[str], None] = print,
    pause: Callable[[str], object] | None = None,
) -> None:
    """Run every check in a new folder of ``notebook``. Raises on the first failure.

    With ``pause``, also asks the person running it to edit a preview in
    LabArchives (``pause`` shows the instructions and waits).
    """
    registry_module.default_registry.register(SMOKE_SCHEMA_ID, 1, SmokePayload)

    notebooks = {c.name: c for c in session.list_containers(ROOT)}
    if notebook not in notebooks:
        raise SmokeFailure(
            f"No notebook named {notebook!r}. Your notebooks: {', '.join(sorted(notebooks))}."
        )
    say(f"ok   found notebook {notebook!r}")
    _probe_missing_folder(session, notebooks[notebook].native_id, say)

    run = datetime.now().strftime("run %Y-%m-%d %H.%M.%S")
    folder = LogicalPath((notebook, SMOKE_FOLDER, run))
    session.ensure_container(folder)
    _check(
        folder in {c.path for c in session.list_containers(folder.parent)},
        "the new folder is not listed",
    )
    say(f"ok   created folder {str(folder)!r}")

    first = _record("first", 1.0)
    receipt = session.write_record(folder, first)
    stored = session.read_record(folder, first.record_id)
    _check(stored.record.payload == first.payload, "the record read back differs")
    _check(stored.receipt.checksum == receipt.checksum, "the read checksum differs")
    say("ok   wrote a record and read it back")

    _check(
        "preview_entry_id" in stored.receipt.backend_ids,
        "MetaRelay could not find the preview entry it just wrote",
    )
    kept = _marker_kept(session, folder, first.record_id)
    if kept is True:
        say("info LabArchives keeps HTML comments in entries")
    elif kept is False:
        say("info LabArchives removes HTML comments from entries (fine: the caption finds them)")
    say("ok   MetaRelay finds its preview entry again")

    again = session.write_record(folder, _record("first", 1.0))
    _check(again.conflict_token == receipt.conflict_token, "rewriting the same record changed it")
    say("ok   writing the same record again changes nothing")

    updated = session.write_record(folder, _record("second", 2.0), prior_receipt=receipt)
    stored = session.read_record(folder, first.record_id)
    _check(stored.record.payload["label"] == "second", "the update was not saved")
    say("ok   updated the record")

    try:
        session.write_record(folder, _record("someone else", 3.0))
    except ConflictError:
        say("ok   a write without a receipt is refused as a conflict")
    else:
        raise SmokeFailure("a conflicting write was not refused")

    _check(
        updated.preview_checksum is not None
        and stored.receipt.preview_checksum == updated.preview_checksum,
        "the preview checksum is missing or unstable",
    )
    _check(
        updated.backend_ids.get("preview_entry_id") == receipt.backend_ids["preview_entry_id"],
        f"the update added a second preview entry to the {RECORDS_PAGE} page "
        "instead of changing the first",
    )
    say("ok   the readable preview is tracked and updated in place")

    if pause is not None:
        pause(
            f"\nIn LabArchives, open the {RECORDS_PAGE!r} page in {str(folder)!r}, change "
            "any text in the table entry for smoke-1, save it, then press Enter here. "
        )
        try:
            session.write_record(folder, _record("third", 4.0), prior_receipt=updated)
        except ConflictError:
            say("ok   a preview edited in LabArchives is not overwritten")
        else:
            raise SmokeFailure("the edit made in LabArchives was overwritten")

    data = b"time,signal\n0,1.5\n"
    file_receipt = session.write_file(folder, "trace.csv", data, media_type="text/csv")
    _check(file_receipt.checksum == hashlib.sha256(data).hexdigest(), "file checksum differs")
    say("ok   uploaded a file")

    records = session.list_records(folder)
    _check([item.record.record_id for item in records] == ["smoke-1"], "files listed as records")
    say("ok   listing finds exactly the one record")

    page = folder / SMOKE_PAGE
    container = session.ensure_container(page, holds_records=True)
    _check(page in {c.path for c in session.list_containers(folder)}, "the page is not listed")
    on_page = session.write_record(page, _record("on a page", 5.0))
    _check(on_page.backend_ids.get("page_id") == container.native_id, "the record missed the page")
    _check(
        session.read_record(page, "smoke-1").record.payload["label"] == "on a page",
        "the record on the page reads back differently",
    )
    try:
        session.ensure_container(page / "inside")
    except ValidationError:
        pass
    else:
        raise SmokeFailure("a folder was created inside a page")
    say(f"ok   a page ({str(page)!r}) holds records and can't contain folders")

    say(f"All smoke checks passed. Remove {str(folder)!r} in LabArchives when done.")


def _marker_kept(session: Session, folder: LogicalPath, record_id: str) -> bool | None:
    """Whether the preview entry still starts with its marker comment.

    ``None`` if ``session`` isn't a LabArchives session.
    """
    if not isinstance(session, LabArchivesSession):
        return None
    # Outside the backend's own methods, so translate labapi and network
    # errors here too: a raw ``requests`` error would print the signed URL.
    with _labarchives_errors("check the preview marker"):
        _, node = session._walk(folder, create=False)  # type: ignore[attr-defined]
        page = session._records_page(node, create=False)  # type: ignore[attr-defined]
        _, preview, _ = session._find(page, record_id)  # type: ignore[attr-defined]
    return preview is not None and preview.content.startswith(_marker(record_id))


def _probe_missing_folder(session: Session, notebook_id: str | None, say) -> None:
    """Ask LabArchives for a folder that doesn't exist, for the cassette.

    MetaRelay finds things by name, so it never sends a bad ID itself; this
    records the error code LabArchives uses, which the fake has to match.
    """
    if not isinstance(session, LabArchivesSession):
        say("skip not a LabArchives session, so no missing-folder probe")
        return
    user = session._user  # the labapi user behind the session
    try:
        user.api_get("tree_tools/get_tree_level", nbid=notebook_id, parent_tree_id=MISSING_TREE_ID)
    except labapi.AuthenticationError:
        raise SmokeFailure("LabArchives rejected the credentials during the probe.") from None
    except labapi.ApiError as error:
        say(f"info LabArchives answers a missing folder with error code {error.error_code}")
    except (labapi.LabArchivesError, requests.RequestException) as error:
        # Never print the error itself: a requests error carries the signed URL.
        raise SmokeFailure(f"The missing-folder probe failed ({type(error).__name__}).") from None
    else:
        say("info LabArchives answers a missing folder without an error")


def _follow_redirect(auth_url: str) -> str:
    """Visit the login page without following its redirect (the fake logs in at once)."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        urllib.request.build_opener(NoRedirect).open(auth_url)
    except urllib.error.HTTPError as error:
        if error.code in {301, 302, 303, 307} and error.headers.get("Location"):
            return error.headers["Location"]
        raise SmokeFailure(
            f"The login page answered {error.code}: LabArchives rejected the credentials. "
            "Check METARELAY_LABARCHIVES_AKID and METARELAY_LABARCHIVES_AKPASS."
        ) from None
    raise SmokeFailure("The login page did not redirect.")


def _login(uri: str, callback_url: str, auto: bool) -> Session:
    challenge = begin_login(uri, callback_url)
    if auto:
        redirected = _follow_redirect(challenge.auth_url)
    else:
        print("Open this link and log in to LabArchives:\n")
        print(f"  {challenge.auth_url}\n")
        redirected = input("Paste the address your browser was sent back to: ").strip()
    params = dict(parse_qsl(urlsplit(redirected).query))
    return complete_login(challenge, params)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m metarelay.testing.labarchives_smoke",
        description="Check the LabArchives backend against a real (or fake) LabArchives.",
    )
    parser.add_argument("--uri", default="labarchives://api.labarchives.com")
    parser.add_argument("--notebook", required=True, help="an existing notebook to write into")
    parser.add_argument("--callback-url", default="http://localhost:8089/callback")
    parser.add_argument("--record", type=Path, help="save the traffic to this cassette file")
    parser.add_argument(
        "--auto-login", action="store_true", help="follow the login redirect (the fake only)"
    )
    parser.add_argument(
        "--check-edits",
        action="store_true",
        help="pause so you can edit a preview in LabArchives, then check it isn't overwritten",
    )
    args = parser.parse_args(argv)
    pause = input if args.check_edits else None

    recording = record_http(args.record) if args.record else contextlib.nullcontext()
    try:
        with recording, _closing(_login(args.uri, args.callback_url, args.auto_login)) as session:
            run_smoke(session, args.notebook, pause=pause)
    except (MetaRelayError, SmokeFailure) as error:
        print(f"Smoke test failed: {error}", file=sys.stderr)
        return 1
    return 0


@contextlib.contextmanager
def _closing(session: Session) -> Iterator[Session]:
    try:
        yield session
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
