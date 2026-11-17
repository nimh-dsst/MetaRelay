"""The eLabFTW backend.

The live tests need a real eLabFTW (the dev stack in ``compose.yaml``)::

    docker compose up -d --wait elabftw
    docker compose exec elabftw bin/init db:populate /seed/populate.yml --yes --fast
    export METARELAY_ELABFTW_TEST_API_URL=http://localhost:3148/api/v2
    pytest tests/backends/test_elabftw.py

Without ``METARELAY_ELABFTW_TEST_API_URL`` they are skipped. Each test works
under its own uniquely named top-level container, so runs never collide.
"""

from __future__ import annotations

import contextlib
import json
import os
import urllib.request
import uuid
from urllib.parse import urlsplit

import pydantic
import pytest

pytest.importorskip("elabapi_python")

from metarelay import connect
from metarelay.backends import elabftw as backend
from metarelay.core.auth import AuthFlow
from metarelay.core.backend import auth_flow
from metarelay.core.conflict import WriteMode
from metarelay.core.errors import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from metarelay.core.path import LogicalPath
from metarelay.core.record import Record
from metarelay.testing import BackendConformance

API_URL = os.environ.get("METARELAY_ELABFTW_TEST_API_URL")
TOKEN = os.environ.get("METARELAY_ELABFTW_TEST_TOKEN", "metarelay-dev-key")
live = pytest.mark.skipif(
    not API_URL, reason="set METARELAY_ELABFTW_TEST_API_URL to run against eLabFTW"
)


def uri_for(kind: str = "experiments") -> str:
    return f"elabftw://{urlsplit(API_URL or 'http://localhost').netloc}/{kind}"


def config(token: str = TOKEN) -> backend.ElabFTWConfig:
    return backend.ElabFTWConfig(token=token, api_url=API_URL)


def api(method: str, path: str, payload: dict | None = None) -> dict | None:
    """Call eLabFTW directly, as a person using the web app would."""
    request = urllib.request.Request(
        f"{API_URL}/{path}",
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        text = response.read()
    return json.loads(text) if text else None


def entry(receipt) -> dict:
    found = api("GET", f"{receipt.backend_ids['entity']}/{receipt.backend_ids['entry_id']}")
    found["metadata"] = json.loads(found["metadata"])
    return found


def unique(prefix: str) -> LogicalPath:
    return LogicalPath((f"{prefix} {uuid.uuid4().hex[:10]}",))


@live
class TestElabFTWExperiments(BackendConformance):
    kind = "experiments"
    records_only_is_a_leaf = True

    @pytest.fixture
    def session(self):
        # The suite reuses names like "Study"; a fresh base keeps runs apart.
        self.base = unique("MetaRelay conformance")
        session = connect(uri_for(self.kind), config())
        session.ensure_container(self.base)
        yield session
        session.close()

    def edit_preview(self, session, path, record_id, text) -> None:
        receipt = session.read_record(path, record_id).receipt
        entity, entry_id = receipt.backend_ids["entity"], receipt.backend_ids["entry_id"]
        api("PATCH", f"{entity}/{entry_id}", {"body": text})


@live
class TestElabFTWResources(TestElabFTWExperiments):
    kind = "resources"


# Connecting


def test_uses_the_token_flow() -> None:
    assert auth_flow("elabftw://elab.example.org/experiments") is AuthFlow.TOKEN


@pytest.mark.parametrize(
    "uri",
    [
        "elabftw://elab.example.org",
        "elabftw://elab.example.org/api/v2",
        "elabftw://elab.example.org/experiments?x=1",
        "elabftw:///experiments",
    ],
)
def test_destination_must_name_the_entry_type(uri: str) -> None:
    with pytest.raises(ConfigurationError, match="experiments"):
        connect(uri, backend.ElabFTWConfig(token="t"))


def test_config_needs_an_elabftw_config() -> None:
    from metarelay.core.config import TokenConfig

    with pytest.raises(ConfigurationError, match="ElabFTWConfig"):
        connect("elabftw://elab.example.org/experiments", TokenConfig(token="t"))


def test_api_url_must_be_http() -> None:
    with pytest.raises(ConfigurationError):
        backend.ElabFTWConfig(token="t", api_url="ftp://elab.example.org")


@live
def test_wrong_key_is_an_authentication_error() -> None:
    with pytest.raises(AuthenticationError) as caught:
        connect(uri_for(), config(token="not-a-key"))
    assert "not-a-key" not in str(caught.value)


# Names and lookups (no server needed)


def test_tags_are_every_prefix() -> None:
    assert backend._tags(LogicalPath(("Study", "Cohort A", "M1"))) == [
        "Study",
        "Study/Cohort A",
        "Study/Cohort A/M1",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Study/Cohort A", '"Study/Cohort A"'),
        ('Say "hi"', '"Say \\"hi\\""'),
        ("back\\slash", '"back_slash"'),  # a backslash would match nothing
    ],
)
def test_search_literals(value: str, expected: str) -> None:
    assert backend._literal(value) == expected


def test_pipe_in_a_name_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"\|"):
        backend._check_names(LogicalPath(("a|b",)))


TEST_SCHEMA_ID = "test.elabftw"


class AnyPayload(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")


def make(record_id: str = "rec-1", **payload) -> Record:
    from metarelay.core import registry as registry_module

    registry_module.default_registry.register(TEST_SCHEMA_ID, 1, AnyPayload)
    return Record(
        schema_id=TEST_SCHEMA_ID,
        schema_version=1,
        record_id=record_id,
        context={"cohort": "A", "subject_id": "M1"},
        payload={"label": "first", **payload},
    )


def test_fields_are_grouped_per_record_and_table() -> None:
    records = {"rec-1": make("rec-1"), "rec-2": make("rec-2", label="second")}
    body, fields, elabftw = backend._render(LogicalPath(("S", "Day 1")), "records", records)
    assert body.count("<table") >= 2
    assert fields["MetaRelay path"] == {
        "type": "text",
        "value": "S/Day 1",
        "group_id": 1,
        "position": 0,
        "readonly": True,
    }
    assert fields["MetaRelay parent"]["value"] == "S"
    assert fields["rec-2 · label"]["value"] == "second"
    groups = {group["id"]: group["name"] for group in elabftw["extra_fields_groups"]}
    assert groups[fields["rec-1 · cohort"]["group_id"]] == "rec-1 · Context"
    assert len(set(groups.values())) == len(groups)


def test_same_row_name_in_two_tables_gets_the_table_title() -> None:
    record = make(cohort="payload cohort")
    _, fields, _ = backend._render(LogicalPath(("S",)), "records", {"rec-1": record})
    cohort_labels = sorted(label for label in fields if label.endswith("cohort"))
    assert len(cohort_labels) == 2
    assert all(" · " in label.removeprefix("rec-1 · ") for label in cohort_labels)


# Live behavior beyond the conformance suite


@pytest.fixture
def session():
    if not API_URL:
        pytest.skip("set METARELAY_ELABFTW_TEST_API_URL to run against eLabFTW")
    session = connect(uri_for(), config())
    yield session
    session.close()


@pytest.fixture
def page(session) -> LogicalPath:
    path = unique("MetaRelay test") / "Day 1"
    session.ensure_container(path, holds_records=True)
    return path


@live
def test_entry_shows_tables_fields_and_tags(session, page) -> None:
    receipt = session.write_record(page, make())
    found = entry(receipt)
    assert "<table" in found["body"]
    assert '"label"' not in found["body"]  # tables, never raw JSON
    fields = found["metadata"]["extra_fields"]
    assert fields["rec-1 · label"]["value"] == "first"
    assert fields["MetaRelay path"]["readonly"] is True
    assert found["title"] == "Day 1"
    assert sorted(found["tags"].split("|")) == sorted(backend._tags(page))


@live
def test_two_records_in_one_entry_never_conflict(session, page) -> None:
    first = session.write_record(page, make("rec-1"))
    session.write_record(page, make("rec-2"))
    # rec-2's write changed the entry; rec-1's old receipt must still work.
    session.write_record(page, make("rec-1", label="v2"), prior_receipt=first)
    assert [item.record.record_id for item in session.list_records(page)] == ["rec-1", "rec-2"]


@live
def test_rewriting_the_same_record_is_not_a_conflict(session, page) -> None:
    # eLabFTW rewrites the HTML it is sent; the saved checksum must account for it.
    receipt = session.write_record(page, make(label='<b>not bold</b> & "quoted"'))
    again = session.write_record(page, make(label="v2"), prior_receipt=receipt)
    assert again.preview_checksum == entry(again)["metadata"]["metarelay"]["preview_checksum"]


@live
def test_editing_a_field_by_hand_conflicts(session, page) -> None:
    receipt = session.write_record(page, make())
    entity, entry_id = receipt.backend_ids["entity"], receipt.backend_ids["entry_id"]
    api("PATCH", f"{entity}/{entry_id}", {"action": "updatemetadatafield", "rec-1 · label": "x"})
    with pytest.raises(ConflictError, match="edited directly"):
        session.write_record(page, make(label="v2"), prior_receipt=receipt)
    # It blocks other records in the same entry too, until forced.
    with pytest.raises(ConflictError):
        session.write_record(page, make("rec-2"))
    session.write_record(page, make("rec-2"), mode=WriteMode.FORCE)
    assert entry(receipt)["metadata"]["extra_fields"]["rec-1 · label"]["value"] == "first"


@live
def test_identical_write_without_receipt_skips_an_edited_entry(session, page) -> None:
    receipt = session.write_record(page, make())
    api(
        "PATCH", f"{receipt.backend_ids['entity']}/{receipt.backend_ids['entry_id']}", {"body": "x"}
    )
    session.write_record(page, make())  # same content, no receipt: like decide_write
    assert entry(receipt)["body"] == "x"


@live
def test_keeps_metadata_keys_it_does_not_own(session, page) -> None:
    receipt = session.write_record(page, make())
    found = entry(receipt)
    found["metadata"]["lab_notes"] = {"keep": True}
    api(
        "PATCH",
        f"{receipt.backend_ids['entity']}/{receipt.backend_ids['entry_id']}",
        {"metadata": json.dumps(found["metadata"])},
    )
    session.write_record(page, make(label="v2"), prior_receipt=receipt)
    assert entry(receipt)["metadata"]["lab_notes"] == {"keep": True}


@live
def test_folder_entries_hold_records_too(session) -> None:
    folder = unique("MetaRelay test")
    session.ensure_container(folder / "child")
    record = make()
    session.write_record(folder, record)
    assert [item.record for item in session.list_records(folder)] == [record]
    assert [c.path for c in session.list_containers(folder)] == [folder / "child"]


@live
@pytest.mark.parametrize("name", ["50%", "under_score", "back\\slash", 'say "hi"', "Ünïcode"])
def test_names_that_search_treats_specially(session, name: str) -> None:
    path = unique("MetaRelay test") / name
    created = session.ensure_container(path)
    assert session.ensure_container(path) == created
    assert [c.path for c in session.list_containers(path.parent)] == [path]


@live
def test_names_that_differ_only_in_case_are_different_containers(session) -> None:
    base = unique("MetaRelay test")
    lower = session.ensure_container(base / "m1")
    upper = session.ensure_container(base / "M1")
    assert lower.native_id != upper.native_id
    assert len(session.list_containers(base)) == 2


def copy_entry(entry_id: str) -> str:
    """A second entry with the same path, as two writers racing would make."""
    copy = api("GET", f"experiments/{entry_id}")
    request = urllib.request.Request(
        f"{API_URL}/experiments",
        method="POST",
        data=json.dumps(
            {"title": copy["title"], "body": copy["body"], "metadata": copy["metadata"]}
        ).encode(),
        headers={"Authorization": TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return response.headers["Location"].rsplit("/", 1)[1]


@live
def test_empty_duplicate_entries_settle_on_the_oldest(session) -> None:
    path = unique("MetaRelay test")
    first = session.ensure_container(path)
    copy_entry(first.native_id)
    assert session.ensure_container(path).native_id == first.native_id
    session.write_record(path, make())
    assert len(session.list_records(path)) == 1


@live
def test_duplicate_entry_with_records_is_the_one_used(session) -> None:
    path = unique("MetaRelay test")
    first = session.ensure_container(path)
    copy = copy_entry(first.native_id)
    # Put a record in the newer copy only, as if another writer used it.
    found = api("GET", f"experiments/{copy}")
    metadata = json.loads(found["metadata"])
    metadata["metarelay"]["records"]["rec-1"] = make().model_dump(mode="json")
    api("PATCH", f"experiments/{copy}", {"metadata": json.dumps(metadata)})
    assert session.ensure_container(path).native_id == copy


@live
def test_two_duplicates_with_records_are_a_conflict(session) -> None:
    path = unique("MetaRelay test")
    first = session.ensure_container(path)
    session.write_record(path, make())
    copy_entry(first.native_id)  # the copy carries the record too
    with pytest.raises(ConflictError, match="More than one"):
        session.list_records(path)


@live
def test_racing_ensure_container_settles_on_one_entry() -> None:
    import threading

    path = unique("MetaRelay test")
    ids: list[str] = []

    def ensure() -> None:
        with contextlib.closing(connect(uri_for(), config())) as racer:
            ids.append(racer.ensure_container(path).native_id)

    threads = [threading.Thread(target=ensure) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with contextlib.closing(connect(uri_for(), config())) as session:
        settled = session.ensure_container(path).native_id
        session.write_record(path, make())
    # Racers that each made an entry still all end up on the same one.
    assert ids == [settled] * 3


@live
def test_unreadable_metadata_is_an_error_not_a_new_entry(session) -> None:
    # Someone breaks the JSON in eLabFTW's metadata editor.
    path = unique("MetaRelay test")
    created = session.ensure_container(path)
    found = api("GET", f"experiments/{created.native_id}")
    metadata = json.loads(found["metadata"])
    metadata["metarelay"]["records"] = []
    api("PATCH", f"experiments/{created.native_id}", {"metadata": json.dumps(metadata)})
    with pytest.raises(ValidationError, match="missing or unreadable"):
        session.ensure_container(path)
    with pytest.raises(ValidationError, match=created.native_id):
        session.write_record(path, make())


@live
def test_path_longer_than_a_tag_still_works(session) -> None:
    base = unique("MetaRelay test")
    path = base / ("x" * 120) / ("y" * 120)
    created = session.ensure_container(path)
    assert session.ensure_container(path) == created
    tags = api("GET", f"experiments/{created.native_id}")["tags"].split("|")
    assert len(str(path)) > backend.TAG_MAX
    assert tags == backend._tags(path) == [str(base), str(path.parent)]


@live
def test_file_name_the_os_refuses_is_a_validation_error(session, page) -> None:
    with pytest.raises(ValidationError):
        session.write_file(page, "x" * 300 + ".csv", b"data")


@live
def test_identical_write_with_receipt_conflicts_with_an_edit(session, page) -> None:
    receipt = session.write_record(page, make())
    entity, entry_id = receipt.backend_ids["entity"], receipt.backend_ids["entry_id"]
    api("PATCH", f"{entity}/{entry_id}", {"body": "edited"})
    with pytest.raises(ConflictError, match="edited directly"):
        session.write_record(page, make(), prior_receipt=receipt)


@live
def test_forcing_identical_content_rebuilds_an_edited_entry(session, page) -> None:
    record = make()
    receipt = session.write_record(page, record)
    entity, entry_id = receipt.backend_ids["entity"], receipt.backend_ids["entry_id"]
    api("PATCH", f"{entity}/{entry_id}", {"body": "edited"})
    forced = session.write_record(page, record, mode=WriteMode.FORCE)
    assert "<table" in entry(receipt)["body"]
    # The entry no longer counts as edited.
    session.write_record(page, make(label="v2"), prior_receipt=forced)


@live
def test_write_file_leaves_one_upload(session, page) -> None:
    receipt = session.write_file(page, "trace.csv", b"old")
    session.write_file(page, "trace.csv", b"new")
    entry_id = receipt.container.native_id
    uploads = api("GET", f"experiments/{entry_id}/uploads")
    assert [upload["real_name"] for upload in uploads] == ["trace.csv"]


@live
def test_missing_container_is_not_found_not_created(session) -> None:
    path = unique("MetaRelay test")
    with pytest.raises(NotFoundError):
        session.write_record(path, make())
    assert path not in [c.path for c in session.list_containers()]


@live
def test_names_are_compared_in_nfc(session) -> None:
    base = unique("MetaRelay test")
    decomposed = session.ensure_container(base / "Café")
    composed = session.ensure_container(base / "Café")
    assert composed.native_id == decomposed.native_id
    assert len(session.list_containers(base)) == 1
