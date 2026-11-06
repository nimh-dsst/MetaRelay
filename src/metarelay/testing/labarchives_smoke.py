"""A live smoke test for the LabArchives backend, for DSST to run with real credentials (R1).

Run it against real LabArchives::

    export METARELAY_LABARCHIVES_AKID=...      # the institution's API key pair
    export METARELAY_LABARCHIVES_AKPASS=...
    python -m metarelay.testing.labarchives_smoke --notebook "My test notebook" \\
        --record labarchives-live.json

It prints a login link. Log in, then paste the address the browser was sent
back to (the page itself may fail to load; only its address matters).

It writes into a new folder, ``MetaRelay smoke tests/run <time>``, in the
notebook you name, and checks write, read, update, conflict detection, and
file upload. ``labapi`` cannot delete anything, so remove the folder in
LabArchives afterwards. Use a test notebook.

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

import pydantic

from metarelay.core import registry as registry_module
from metarelay.core.backend import begin_login, complete_login
from metarelay.core.errors import ConflictError, MetaRelayError
from metarelay.core.path import ROOT, LogicalPath
from metarelay.core.record import Record
from metarelay.core.session import Session
from metarelay.testing.labarchives_cassettes import record_http

__all__ = ["SMOKE_SCHEMA_ID", "main", "run_smoke"]

SMOKE_SCHEMA_ID = "metarelay.smoke"
SMOKE_FOLDER = "MetaRelay smoke tests"


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


def run_smoke(session: Session, notebook: str, say: Callable[[str], None] = print) -> None:
    """Run every check in a new folder of ``notebook``. Raises on the first failure."""
    registry_module.default_registry.register(SMOKE_SCHEMA_ID, 1, SmokePayload)

    notebooks = {container.name for container in session.list_containers(ROOT)}
    if notebook not in notebooks:
        raise SmokeFailure(
            f"No notebook named {notebook!r}. Your notebooks: {', '.join(sorted(notebooks))}."
        )
    say(f"ok   found notebook {notebook!r}")

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
    say("ok   the readable preview is tracked")

    data = b"time,signal\n0,1.5\n"
    file_receipt = session.write_file(folder, "trace.csv", data, media_type="text/csv")
    _check(file_receipt.checksum == hashlib.sha256(data).hexdigest(), "file checksum differs")
    say("ok   uploaded a file")

    records = session.list_records(folder)
    _check([item.record.record_id for item in records] == ["smoke-1"], "files listed as records")
    say("ok   listing finds exactly the one record")

    say(f"All smoke checks passed. Remove {str(folder)!r} in LabArchives when done.")


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
    args = parser.parse_args(argv)

    recording = record_http(args.record) if args.record else contextlib.nullcontext()
    try:
        with recording, _closing(_login(args.uri, args.callback_url, args.auto_login)) as session:
            run_smoke(session, args.notebook)
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
