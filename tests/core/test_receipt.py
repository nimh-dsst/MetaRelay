import hashlib
import json

import pytest

from metarelay import (
    Container,
    FileReceipt,
    LogicalPath,
    Receipt,
    SyncIndex,
    ValidationError,
    record_checksum,
)
from metarelay.core.checksum import canonical_json, preview_checksum
from metarelay.core.session import StoredRecord


def make_receipt(record_id: str = "fiber-1", destination: str = "file:///tmp/x") -> Receipt:
    return Receipt(
        destination=destination,
        backend="file",
        container=Container(path=LogicalPath(("Study", "M1"))),
        record_id=record_id,
        schema_id="test.fiber",
        schema_version=1,
        checksum="abc",
        conflict_token="abc",
    )


def test_canonical_json_ignores_key_order_and_whitespace() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1})
    assert canonical_json({"a": "µm"}) == '{"a":"µm"}'.encode()


def test_checksum_uses_chronoxivs_recipe(make_record) -> None:
    record = make_record()
    content = {
        "schema_id": record.schema_id,
        "schema_version": record.schema_version,
        "record_id": record.record_id,
        "context": record.context,
        "payload": record.payload,
    }
    expected = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert record_checksum(record) == expected


def test_checksum_ignores_updated_at(make_record) -> None:
    first = make_record()
    later = first.model_copy(update={"updated_at": first.updated_at.replace(year=2030)})
    assert record_checksum(first) == record_checksum(later)


def test_checksum_changes_with_content(make_record) -> None:
    assert record_checksum(make_record(diameter_um=400)) != record_checksum(
        make_record(diameter_um=200)
    )


def test_preview_checksum_is_stable() -> None:
    assert preview_checksum("<table/>") == preview_checksum("<table/>")
    assert preview_checksum("<table/>") != preview_checksum("<table />")


def test_receipt_round_trips_through_json() -> None:
    receipt = make_receipt()
    assert Receipt.model_validate_json(receipt.model_dump_json()) == receipt
    assert receipt.preview_checksum is None
    assert receipt.backend_ids == {}


def test_sync_index_keys_by_destination_path_and_id() -> None:
    index = SyncIndex()
    local = make_receipt(destination="file:///tmp/x")
    github = make_receipt(destination="github://lab/repo@main/meta")
    index.put(local)
    index.put(github)
    path = LogicalPath(("Study", "M1"))
    assert index.get("file:///tmp/x", path, "fiber-1") == local
    assert index.get("github://lab/repo@main/meta", path, "fiber-1") == github
    assert index.get("file:///tmp/x", path, "other") is None

    index.remove("file:///tmp/x", path, "fiber-1")
    assert index.get("file:///tmp/x", path, "fiber-1") is None


def test_keys_cannot_collide_through_separator_characters() -> None:
    from metarelay.core.receipt import receipt_key

    assert receipt_key("x://a|b", LogicalPath(("c",)), "d") != receipt_key(
        "x://a", LogicalPath(("b|c",)), "d"
    )


def test_sync_index_round_trips_through_json() -> None:
    index = SyncIndex()
    index.put(make_receipt())
    assert SyncIndex.model_validate_json(index.model_dump_json()) == index


def test_equivalent_destination_spellings_find_the_same_receipt() -> None:
    index = SyncIndex()
    index.put(make_receipt(destination="LabArchives://API.Example.org/"))
    found = index.get("labarchives://api.example.org", LogicalPath(("Study", "M1")), "fiber-1")
    assert found is not None
    assert found.destination == "labarchives://api.example.org"


# Every public model loaded from bad data raises metarelay.ValidationError,
# never pydantic's. Add a row whenever a new escape is found.
GOOD_RECEIPT = json.loads(make_receipt().model_dump_json())
MALFORMED = [
    (SyncIndex, {"receipts": {"k": {**GOOD_RECEIPT, "container": {"path": ["a/b"]}}}}),
    (SyncIndex, {"index_version": 2}),
    (Receipt, {**GOOD_RECEIPT, "destination": "not a uri"}),
    (Receipt, {**GOOD_RECEIPT, "checksum": None}),
    (FileReceipt, {"destination": "file:///x"}),
    (StoredRecord, {"record": {}, "receipt": GOOD_RECEIPT}),
    (Container, {"path": "a/b"}),
]


@pytest.mark.parametrize(("model", "data"), MALFORMED)
def test_malformed_data_is_a_metarelay_error(model, data) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(data)
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(data))
