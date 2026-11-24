"""The eLabFTW backend.

The live tests need a real eLabFTW (the dev stack in ``compose.yaml``)::

    docker compose up -d --wait elabftw
    docker compose exec elabftw bin/init db:populate /seed/populate.yml --yes --fast
    export METARELAY_ELABFTW_TEST_API_URL=http://localhost:3148/api/v2
    pytest tests/backends/test_elabftw.py

Without ``METARELAY_ELABFTW_TEST_API_URL`` they are skipped. Each test works
under its own uniquely named entry in the test category (``elabftw_category``
in ``tests/conftest.py``), so runs never collide.
"""

from __future__ import annotations

import contextlib
import json
import os
import types
import urllib.request
import uuid
from urllib.parse import urlsplit

import pydantic
import pytest

pytest.importorskip("elabapi_python")

import urllib3  # a dependency of elabapi-python, so only after the skip
from elabapi_python.rest import ApiException

from metarelay import connect
from metarelay.backends import elabftw as backend
from metarelay.backends import elabftw_client
from metarelay.core import preview
from metarelay.core.conflict import WriteMode, accept_edits
from metarelay.core.errors import (
    AuthenticationError,
    BackendUnavailableError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from metarelay.core.path import LogicalPath
from metarelay.core.preview import PreviewDiff, PreviewEdit
from metarelay.core.receipt import SyncIndex
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


@live
class TestElabFTWExperiments(BackendConformance):
    kind = "experiments"
    records_only_is_a_leaf = True

    @pytest.fixture
    def session(self, elabftw_category):
        # The suite reuses names like "Study"; a fresh base keeps runs apart.
        self.base = LogicalPath((elabftw_category, f"MetaRelay conformance {uuid.uuid4().hex}"))
        session = connect(uri_for(self.kind), config())
        session.ensure_container(self.base)
        yield session
        session.close()

    def edit_preview(self, session, path, record_id, text) -> None:
        receipt = session.read_record(path, record_id).receipt
        entity, entry_id = receipt.backend_ids["entity"], receipt.backend_ids["entry_id"]
        api("PATCH", f"{entity}/{entry_id}", {"body": text})

    def replace_in_preview(self, session, path, record_id, old, new) -> None:
        receipt = session.read_record(path, record_id).receipt
        fields = entry(receipt)["metadata"]["extra_fields"]
        [label] = [label for label, field in fields.items() if field["value"] == old]
        entity, entry_id = receipt.backend_ids["entity"], receipt.backend_ids["entry_id"]
        api("PATCH", f"{entity}/{entry_id}", {"action": "updatemetadatafield", label: new})


@live
class TestElabFTWResources(TestElabFTWExperiments):
    """Resources share the experiments code path; only the entity differs.

    So run a sample of the suite that touches every entity-specific call
    (containers, records, files, previews) rather than all of it again.
    """

    kind = "resources"


_RESOURCES_SAMPLE = {
    "test_write_then_read",
    "test_upsert_with_receipt_updates",
    "test_missing_container_is_not_found",
    "test_records_only_container_holds_records",
    "test_container_inside_records_only_container",
    "test_preview_edit_says_what_changed",
    "test_write_file",
}
for _name in [n for n in dir(TestElabFTWResources) if n.startswith("test_")]:
    if _name not in _RESOURCES_SAMPLE:
        setattr(TestElabFTWResources, _name, None)


def test_the_resources_sample_names_only_real_conformance_tests() -> None:
    # A misspelled or renamed name would otherwise drop a test silently.
    suite = {name for name in dir(BackendConformance) if name.startswith("test_")}
    assert not _RESOURCES_SAMPLE - suite, _RESOURCES_SAMPLE - suite


# Errors. A real server can't be made to fail on demand, so these drive the
# adapter's error translation directly.


def api_error(status: int, body: bytes | None = None) -> ApiException:
    error = ApiException(status=status, reason="nope")
    error.body = body
    return error


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ValidationError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (500, BackendUnavailableError),
        (503, BackendUnavailableError),
    ],
)
def test_http_errors_become_metarelay_errors(status: int, expected: type[Exception]) -> None:
    body = json.dumps({"message": "Forbidden", "description": "not your entry"}).encode()
    with pytest.raises(expected) as raised, elabftw_client._errors("read the entry"):
        raise api_error(status, body)
    assert str(raised.value) == (
        f"eLabFTW could not read the entry (HTTP {status}): Forbidden not your entry"
    )
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


@pytest.mark.parametrize("body", [None, b"<html>Bad gateway</html>", b"[1, 2]"])
def test_an_error_body_that_is_not_elabftw_json_is_left_out(body: bytes | None) -> None:
    with pytest.raises(BackendUnavailableError) as raised, elabftw_client._errors("search"):
        raise api_error(502, body)
    assert str(raised.value) == "eLabFTW could not search (HTTP 502)"


def test_a_network_error_is_unavailable_and_not_chained() -> None:
    # urllib3 errors can carry the request, and the request carries the API key.
    with pytest.raises(BackendUnavailableError) as raised, elabftw_client._errors("log in"):
        raise urllib3.exceptions.MaxRetryError(None, f"/api/v2?key={TOKEN}")
    assert str(raised.value) == "Could not reach eLabFTW to log in (MaxRetryError)."
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_a_response_it_cannot_parse_is_unavailable() -> None:
    with (
        pytest.raises(BackendUnavailableError, match="did not understand"),
        elabftw_client._errors("create an entry"),
    ):
        raise ValueError("no Location header")


def test_an_unreachable_server_is_unavailable() -> None:
    # Nothing listens on port 9 (discard) here; the client must not hang or leak.
    client = elabftw_client.ElabClient(
        "http://127.0.0.1:9/api/v2", "secret-key", entity="experiments", verify_tls=True
    )
    client._client.configuration.retries = 0
    with pytest.raises(BackendUnavailableError, match="Could not reach eLabFTW") as raised:
        client.check_login()
    assert "secret-key" not in str(raised.value)


def test_search_pages_through_the_team_in_id_order() -> None:
    client = elabftw_client.ElabClient(
        "http://127.0.0.1:9/api/v2", "key", entity="experiments", verify_tls=True
    )
    size = elabftw_client.PAGE_SIZE
    pages = [[{"id": n} for n in range(size)], [{"id": size}]]
    calls: list[dict] = []

    def fake_list(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(data=json.dumps(pages[len(calls) - 1]).encode())

    client._list = fake_list
    assert client.search("tag:x") == [{"id": n} for n in range(size + 1)]
    # The whole team's entries, in a stable order, so paging by offset can't
    # skip an entry that another writer changes meanwhile.
    assert [(c["scope"], c["order"], c["sort"]) for c in calls] == [
        (elabftw_client.SCOPE_TEAM, "id", "asc")
    ] * 2
    assert [(c["limit"], c["offset"]) for c in calls] == [(size, 0), (size, size)]


# Connecting


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
    records = {
        "rec-1": make("rec-1"),
        "rec-2": make("rec-2", label="second", cohort="payload cohort"),
    }
    body, fields, elabftw, _ = backend._render(LogicalPath(("S", "Day 1")), "records", records)
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
    # rec-2 has a cohort row in two tables: each label gets its table title.
    cohort_labels = [n for n in fields if n.startswith("rec-2 · ") and n.endswith("cohort")]
    assert len(cohort_labels) == 2
    assert all(" · " in label.removeprefix("rec-2 · ") for label in cohort_labels)


def _saved_entry(records: dict) -> tuple[dict, dict, LogicalPath]:
    """An entry as MetaRelay saved it, and its ``metadata.metarelay``."""
    path = LogicalPath(("S",))
    body, fields, _, _ = backend._render(path, "records", records)
    ours = {"kind": "records", "preview_checksum": backend._entry_checksum(body, fields)}
    return {"body": body, "metadata": {"extra_fields": fields}}, ours, path


def _diff(change) -> PreviewDiff:
    records = {"rec-1": make("rec-1"), "rec-2": make("rec-2", count="3")}
    entry, ours, path = _saved_entry(records)
    change(entry, entry["metadata"]["extra_fields"])
    return backend._entry_diff(entry, ours, path, records)


def _edit(record_id, field, written, current) -> PreviewEdit:
    return PreviewEdit(
        record_id=record_id, table="Metadata", field=field, written=written, current=current
    )


def test_field_diff_lists_a_removed_field_as_complete() -> None:
    diff = _diff(lambda entry, fields: fields.pop("rec-1 · label"))
    assert diff == PreviewDiff(edits=[_edit("rec-1", "label", "first", None)], complete=True)


def test_field_diff_keeps_non_text_values() -> None:
    diff = _diff(lambda entry, fields: fields["rec-2 · count"].update(value=0))
    assert diff.edits == [_edit("rec-2", "count", "3", "0")]


@pytest.mark.parametrize(
    "change",
    [
        lambda entry, fields: entry.update(body=entry["body"] + "<p>note</p>"),
        lambda entry, fields: fields.update({"Notes": {"type": "text", "value": "x"}}),
        lambda entry, fields: fields["MetaRelay path"].update(value="Elsewhere"),
        lambda entry, fields: fields["rec-1 · label"].update(type="number"),
    ],
    ids=["body", "added field", "path field", "field settings"],
)
def test_field_diff_is_incomplete_when_something_else_changed(change) -> None:
    diff = _diff(change)
    assert diff == PreviewDiff(edits=[], complete=False)


def _in_body(entry, old, new, count=1) -> None:
    """Change a body cell the way eLabFTW's editor saves it: restyled HTML."""
    assert old in entry["body"]
    entry["body"] = entry["body"].replace(old, new, count).replace("<td>", '<td style="w:1">')


def test_entry_diff_lists_the_same_change_in_body_and_field_once() -> None:
    def change(entry, fields):
        _in_body(entry, "<td>3</td>", "<td>4</td>")
        fields["rec-2 · count"].update(value="4")

    assert _diff(change) == PreviewDiff(edits=[_edit("rec-2", "count", "3", "4")], complete=True)


def test_entry_diff_is_incomplete_when_body_and_field_disagree() -> None:
    def change(entry, fields):
        _in_body(entry, "<td>3</td>", "<td>4</td>")
        fields["rec-2 · count"].update(value="5")

    diff = _diff(change)
    assert diff.edits == [_edit("rec-2", "count", "3", "5"), _edit("rec-2", "count", "3", "4")]
    assert not diff.complete


def test_entry_diff_ignores_field_positions() -> None:
    def change(entry, fields):
        for position, field in enumerate(reversed(fields.values())):
            field["position"] = position

    assert _diff(change) == PreviewDiff(edits=[], complete=True)


def test_body_diff_without_records_is_incomplete_when_a_table_was_added() -> None:
    # A table parses cleanly, so only the block count catches it.
    table = "<table><tr><td>a</td><td>b</td></tr></table>"
    assert backend._body_diff("", {}) == PreviewDiff(edits=[], complete=True)
    assert backend._body_diff(table, {}) == PreviewDiff(edits=[], complete=False)


def test_entry_diff_matches_a_multiline_value_in_body_and_field() -> None:
    # The body is read by its text, which collapses whitespace; the field
    # keeps the raw value. Both must read the same to be matched.
    records = {"rec-1": make("rec-1", label="two\nlines")}
    entry, ours, path = _saved_entry(records)
    entry["body"] = entry["body"].replace("two\nlines", "edited")
    entry["metadata"]["extra_fields"]["rec-1 · label"].update(value="other")
    diff = backend._entry_diff(entry, ours, path, records)
    assert diff.edits == [
        _edit("rec-1", "label", "two lines", "other"),
        _edit("rec-1", "label", "two lines", "edited"),
    ]
    assert not diff.complete


def test_entry_diff_ignores_whitespace_only_field_changes() -> None:
    diff = _diff(lambda entry, fields: fields["rec-2 · label"].update(value=" first "))
    assert diff == PreviewDiff(edits=[], complete=True)


def test_body_diff_splits_a_preview_with_its_own_layout(monkeypatch) -> None:
    # A schema's own renderer may use several headings, or none.
    def two_headings(record):
        return [
            preview.Heading(text="Part one"),
            preview.Heading(text=record.record_id),
            preview.Table(title="Metadata", rows=[("label", record.payload["label"])]),
        ]

    records = {"rec-1": make("rec-1"), "rec-2": make("rec-2")}
    monkeypatch.setitem(preview._renderers, TEST_SCHEMA_ID, two_headings)
    entry, _, _ = _saved_entry(records)
    assert backend._body_diff(entry["body"], records) == PreviewDiff(edits=[], complete=True)
    _in_body(entry, "<td>first</td>", "<td>by hand</td>")
    diff = backend._body_diff(entry["body"], records)
    assert diff == PreviewDiff(edits=[_edit("rec-1", "label", "first", "by hand")], complete=True)


def test_entry_diff_is_incomplete_when_a_record_section_is_missing() -> None:
    def change(entry, fields):
        entry["body"] = entry["body"].split("<hr>")[0]

    assert not _diff(change).complete


# Live behavior beyond the conformance suite


@pytest.fixture
def session():
    if not API_URL:
        pytest.skip("set METARELAY_ELABFTW_TEST_API_URL to run against eLabFTW")
    session = connect(uri_for(), config())
    yield session
    session.close()


@pytest.fixture
def top(elabftw_category) -> LogicalPath:
    """A fresh entry path in the test category."""
    return LogicalPath((elabftw_category, f"MetaRelay test {uuid.uuid4().hex[:10]}"))


@pytest.fixture
def page(session, top) -> LogicalPath:
    path = top / "Day 1"
    session.ensure_container(path, holds_records=True)
    return path


@live
def test_entry_shows_tables_fields_and_category(session, page, elabftw_category) -> None:
    receipt = session.write_record(page, make())
    found = entry(receipt)
    assert "<table" in found["body"]
    assert '"label"' not in found["body"]  # tables, never raw JSON
    fields = found["metadata"]["extra_fields"]
    assert fields["rec-1 · label"]["value"] == "first"
    assert fields["MetaRelay path"]["readonly"] is True
    assert found["title"] == "Day 1"
    # Folder and record entries alike are in the path's category, with no path tags.
    folder = api("GET", f"experiments/{session.ensure_container(page.parent).native_id}")
    assert found["category_title"] == folder["category_title"] == elabftw_category
    assert found["tags"] is folder["tags"] is None


@live
def test_categories_are_the_top_level(session, elabftw_category) -> None:
    category = LogicalPath((elabftw_category,))
    assert category in [c.path for c in session.list_containers()]
    assert session.ensure_container(category).path == category
    assert session.list_records(category) == []
    with pytest.raises(ValidationError, match="inside a category"):
        session.write_record(category, make())


@live
def test_a_missing_category_is_not_found_not_created(session) -> None:
    category = LogicalPath((f"MetaRelay missing {uuid.uuid4().hex[:10]}",))
    with pytest.raises(NotFoundError, match=f"No category {category.name!r}"):
        session.ensure_container(category / "Study")
    with pytest.raises(NotFoundError, match=f"No category {category.name!r}"):
        session.list_containers(category)
    assert category not in [c.path for c in session.list_containers()]


def test_category_titles_are_matched_and_listed() -> None:
    categories = [
        {"id": 1, "title": "Twice"},
        {"id": 2, "title": "Twice"},
        {"id": 3, "title": "1/2"},
    ]
    client = types.SimpleNamespace(categories=lambda: categories)
    session = backend.ElabFTWSession(client, "elabftw://elab.example.org/experiments", {})
    # A title a path can't hold is left out of the list, not an error.
    assert [c.path for c in session.list_containers()] == [LogicalPath(("Twice",))] * 2
    with pytest.raises(ConflictError, match="More than one eLabFTW category"):
        session.ensure_container(LogicalPath(("Twice", "Study")))


@live
def test_a_record_write_adds_its_mapped_tag_and_keeps_others(page) -> None:
    tags = backend.ElabFTWConfig(
        token=TOKEN, api_url=API_URL, record_tags={TEST_SCHEMA_ID: "Surgeries"}
    )
    with contextlib.closing(connect(uri_for(), tags)) as session:
        receipt = session.write_record(page, make())
        entry_id = receipt.backend_ids["entry_id"]
        api("POST", f"experiments/{entry_id}/tags", {"tag": "Reviewed"})  # a person adds one
        receipt = session.write_record(page, make(label="v2"), prior_receipt=receipt)
        assert sorted(entry(receipt)["tags"].split("|")) == ["Reviewed", "Surgeries"]


@live
def test_moving_or_retagging_an_entry_is_not_a_conflict(session, page) -> None:
    receipt = session.write_record(page, make())
    entry_id = receipt.backend_ids["entry_id"]
    # A person takes the entry out of its category and tags it.
    api("PATCH", f"experiments/{entry_id}", {"category": None})
    api("POST", f"experiments/{entry_id}/tags", {"tag": "Reviewed"})
    receipt = session.write_record(page, make(label="v2"), prior_receipt=receipt)
    assert receipt.backend_ids["entry_id"] == entry_id  # found by path, not moved back
    assert entry(receipt)["category"] is None


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
def test_hand_edits_to_an_entry_conflict(session, page) -> None:
    receipt = session.write_record(page, make("rec-1"))
    session.write_record(page, make("rec-2", site="NAc"))
    entity, entry_id = receipt.backend_ids["entity"], receipt.backend_ids["entry_id"]

    def write_rec_1():
        with pytest.raises(ConflictError, match="edited directly") as raised:
            session.write_record(page, make("rec-1", label="v2"), prior_receipt=receipt)
        return raised.value

    # 1. A field of the record being written, edited by hand.
    api("PATCH", f"{entity}/{entry_id}", {"action": "updatemetadatafield", "rec-1 · label": "x"})
    assert "label: 'first' → 'x'" in str(write_rec_1())  # its own edit: not named

    # 2. A field of another record in the same entry. The message names
    # rec-2, so it doesn't read as a change to rec-1.
    api("PATCH", f"{entity}/{entry_id}", {"action": "updatemetadatafield", "rec-2 · site": "VTA"})
    conflict = write_rec_1()
    assert "site of rec-2: 'NAc' → 'VTA'" in str(conflict)
    assert conflict.preview_diff == PreviewDiff(
        edits=[
            _edit("rec-1", "label", "first", "x"),
            PreviewEdit(
                record_id="rec-2", table="Metadata", field="site", written="NAc", current="VTA"
            ),
        ],
        complete=True,
    )

    # 3. A note added to the body: the field edits are still listed, but
    # can't be the whole story.
    api("PATCH", f"{entity}/{entry_id}", {"body": entry(receipt)["body"] + "<p>Note</p>"})
    conflict = write_rec_1()
    assert "can't list" in str(conflict)
    diff = conflict.preview_diff
    assert [(edit.field, edit.current) for edit in diff.edits] == [("label", "x"), ("site", "VTA")]
    assert not diff.complete

    # 4. It blocks other records in the same entry too, until forced.
    with pytest.raises(ConflictError):
        session.write_record(page, make("rec-3"))
    session.write_record(page, make("rec-3"), mode=WriteMode.FORCE)
    assert entry(receipt)["metadata"]["extra_fields"]["rec-1 · label"]["value"] == "first"


@live
def test_the_cli_accepts_edits_to_every_record_in_the_entry(
    session, page, tmp_path, monkeypatch
) -> None:
    from metarelay.cli import main

    receipt = session.write_record(page, make("rec-1"))
    session.write_record(page, make("rec-2", count="3"))
    entity, entry_id = receipt.backend_ids["entity"], receipt.backend_ids["entry_id"]
    # A value with doubled spaces, and a field cleared, in another record.
    for label, value in (("rec-1 · label", "by  hand"), ("rec-2 · count", "")):
        api("PATCH", f"{entity}/{entry_id}", {"action": "updatemetadatafield", label: value})
    file = tmp_path / "rec-1.json"
    file.write_text(make("rec-1").model_dump_json())
    receipts = tmp_path / "receipts.json"
    index = SyncIndex()
    index.put(receipt)
    receipts.write_text(index.model_dump_json())
    monkeypatch.setenv("METARELAY_ELABFTW_TOKEN", TOKEN)
    monkeypatch.setenv("METARELAY_ELABFTW_API_URL", API_URL)

    args = ["write", uri_for(), str(page), str(file), "--receipts", str(receipts)]
    assert main([*args, "--accept-edits"]) == 0
    stored = {s.record.record_id: s.record.payload for s in session.list_records(page)}
    assert stored["rec-1"]["label"] == "by hand"
    assert stored["rec-2"]["count"] == ""
    assert json.loads(file.read_text())["payload"]["label"] == "by hand"
    # The entry was rebuilt from the edited records, and the saved receipts
    # describe it: writing either record next goes through.
    saved = SyncIndex.model_validate_json(receipts.read_text())
    rec_2 = saved.get(session.destination, page, "rec-2")
    session.write_record(page, make("rec-2", count="4"), prior_receipt=rec_2)
    assert main(args) == 0


@live
def test_an_edit_to_a_body_table_can_be_accepted(session, page) -> None:
    receipt = session.write_record(page, make())
    entity, entry_id = receipt.backend_ids["entity"], receipt.backend_ids["entry_id"]
    body = entry(receipt)["body"]
    assert body.count("<td>first</td>") == 1
    # Like eLabFTW's editor: restyled cells, the value wrapped in <p>.
    edited = body.replace("<td>first</td>", '<td style="width:30%;"><p>by hand</p></td>')
    api("PATCH", f"{entity}/{entry_id}", {"body": edited})
    with pytest.raises(ConflictError, match="label: 'first' → 'by hand'") as raised:
        session.write_record(page, make(label="v2"), prior_receipt=receipt)
    diff = raised.value.preview_diff
    assert diff == PreviewDiff(edits=[_edit("rec-1", "label", "first", "by hand")], complete=True)
    written = accept_edits(session, page, make(), diff)
    assert written["rec-1"].record.payload["label"] == "by hand"
    assert entry(receipt)["metadata"]["extra_fields"]["rec-1 · label"]["value"] == "by hand"


@live
def test_identical_content_in_an_edited_entry(session, page) -> None:
    # Core TestEntryEdits owns the decision; this checks eLabFTW carries it out.
    record = make()
    receipt = session.write_record(page, record)
    entity, entry_id = receipt.backend_ids["entity"], receipt.backend_ids["entry_id"]
    api("PATCH", f"{entity}/{entry_id}", {"body": "edited"})
    # No receipt: skipped, like decide_write, and the edit is left alone.
    session.write_record(page, record)
    assert entry(receipt)["body"] == "edited"
    # With the receipt: a conflict.
    with pytest.raises(ConflictError, match="edited directly"):
        session.write_record(page, record, prior_receipt=receipt)
    # Forced: the entry is rebuilt.
    forced = session.write_record(page, record, mode=WriteMode.FORCE)
    assert "<table" in entry(receipt)["body"]
    # The entry no longer counts as edited.
    session.write_record(page, make(label="v2"), prior_receipt=forced)


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
def test_folder_entries_hold_records_too(session, top) -> None:
    session.ensure_container(top / "child")
    record = make()
    session.write_record(top, record)
    assert [item.record for item in session.list_records(top)] == [record]
    assert [c.path for c in session.list_containers(top)] == [top / "child"]


@live
@live
def test_names_that_search_treats_specially(session, top) -> None:
    # eLabFTW's search is SQL LIKE: each of these names, sent through a real
    # search, must still find exactly its own container.
    for i, name in enumerate(
        (
            "50%",  # % is a LIKE wildcard
            "under_score",  # so is _
            "back\\slash",  # a backslash goes wrong in LIKE and in the query
            'say "hi"',  # quotes end the search literal
            "Ünïcode",
            "a|b",
        )
    ):
        path = top / f"names {i}" / name
        created = session.ensure_container(path)
        assert session.ensure_container(path) == created, name
        assert [c.path for c in session.list_containers(path.parent)] == [path], name

    # LIKE ignores case, but names that differ only in case are different
    # containers.
    base = top / "case"
    lower = session.ensure_container(base / "m1")
    upper = session.ensure_container(base / "M1")
    assert lower.native_id != upper.native_id
    assert len(session.list_containers(base)) == 2

    # Names are compared in NFC: a decomposed and a composed "Café" are one
    # container.
    base = top / "nfc"
    decomposed = session.ensure_container(base / "Cafe\u0301")
    composed = session.ensure_container(base / "Caf\u00e9")
    assert composed.native_id == decomposed.native_id
    assert len(session.list_containers(base)) == 1


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
def test_empty_duplicate_entries_settle_on_the_oldest(session, top) -> None:
    first = session.ensure_container(top)
    copy_entry(first.native_id)
    assert session.ensure_container(top).native_id == first.native_id
    session.write_record(top, make())
    assert len(session.list_records(top)) == 1


@live
def test_duplicate_entry_with_records_is_the_one_used(session, top) -> None:
    first = session.ensure_container(top)
    copy = copy_entry(first.native_id)
    # Put a record in the newer copy only, as if another writer used it.
    found = api("GET", f"experiments/{copy}")
    metadata = json.loads(found["metadata"])
    metadata["metarelay"]["records"]["rec-1"] = make().model_dump(mode="json")
    api("PATCH", f"experiments/{copy}", {"metadata": json.dumps(metadata)})
    assert session.ensure_container(top).native_id == copy


@live
def test_two_duplicates_with_records_are_a_conflict(session, top) -> None:
    first = session.ensure_container(top)
    session.write_record(top, make())
    copy_entry(first.native_id)  # the copy carries the record too
    with pytest.raises(ConflictError, match="More than one"):
        session.list_records(top)


@live
def test_racing_ensure_container_settles_on_one_entry(top) -> None:
    import threading

    ids: list[str] = []

    def ensure() -> None:
        with contextlib.closing(connect(uri_for(), config())) as racer:
            ids.append(racer.ensure_container(top).native_id)

    threads = [threading.Thread(target=ensure) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with contextlib.closing(connect(uri_for(), config())) as session:
        settled = session.ensure_container(top).native_id
        session.write_record(top, make())
    # Racers that each made an entry still all end up on the same one.
    assert ids == [settled] * 3


@live
def test_unreadable_metadata_is_an_error_not_a_new_entry(session, top) -> None:
    # Someone breaks the JSON in eLabFTW's metadata editor.
    created = session.ensure_container(top)
    found = api("GET", f"experiments/{created.native_id}")
    metadata = json.loads(found["metadata"])
    metadata["metarelay"]["records"] = []
    api("PATCH", f"experiments/{created.native_id}", {"metadata": json.dumps(metadata)})
    with pytest.raises(ValidationError, match="missing or unreadable"):
        session.ensure_container(top)
    with pytest.raises(ValidationError, match=created.native_id):
        session.write_record(top, make())


@live
def test_file_name_the_os_refuses_is_a_validation_error(session, page) -> None:
    with pytest.raises(ValidationError):
        session.write_file(page, "x" * 300 + ".csv", b"data")


@live
def test_write_file_leaves_one_upload(session, page) -> None:
    receipt = session.write_file(page, "trace.csv", b"old")
    session.write_file(page, "trace.csv", b"new")
    entry_id = receipt.container.native_id
    uploads = api("GET", f"experiments/{entry_id}/uploads")
    assert [upload["real_name"] for upload in uploads] == ["trace.csv"]
