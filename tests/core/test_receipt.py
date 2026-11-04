import hashlib
import json
import traceback

import pytest

from metarelay import (
    ConfigurationError,
    Container,
    Destination,
    FileReceipt,
    LogicalPath,
    LoginChallenge,
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


class TestCredentialsNeverEchoed:
    """Review on #17: the error rejecting a password must not print it."""

    SECRET = "hunter2"
    URI = f"labarchives://user:{SECRET}@host"

    def _full_text(self, error: BaseException) -> str:
        # Every message a logged traceback would show: this error and its chain.
        texts = []
        seen: BaseException | None = error
        while seen is not None:
            texts.append(f"{type(seen).__name__}: {seen}")
            # What traceback printing follows: `from None` hides the context.
            hidden = seen.__suppress_context__
            seen = seen.__cause__ or (None if hidden else seen.__context__)
        assert "".join(traceback.format_exception(error))  # still printable
        return "\n".join(texts)

    def test_receipt(self) -> None:
        with pytest.raises(ValidationError) as caught:
            Receipt(
                destination=self.URI,
                backend="labarchives",
                container=Container(path=LogicalPath(("a",))),
                record_id="r",
                schema_id="test.fiber",
                schema_version=1,
                checksum="c",
                conflict_token="c",
            )
        assert "username or password" in str(caught.value)
        assert self.SECRET not in self._full_text(caught.value)

    def test_receipt_loaded_from_json(self) -> None:
        with pytest.raises(ValidationError) as caught:
            receipt = {
                "destination": self.URI,
                "backend": "x",
                "container": {},
                "record_id": "r",
                "schema_id": "a",
                "schema_version": 1,
                "checksum": "c",
                "conflict_token": "c",
            }
            SyncIndex.model_validate_json(json.dumps({"receipts": {"k": receipt}}))
        assert self.SECRET not in self._full_text(caught.value)

    def test_login_challenge(self) -> None:
        with pytest.raises(ValidationError) as caught:
            LoginChallenge(destination=self.URI, auth_url="https://x", redirect_url="https://y")
        assert self.SECRET not in self._full_text(caught.value)

    @pytest.mark.parametrize(
        "uri",
        [
            f"user:{SECRET}@host",  # no scheme: used to hit "is not a URI" first
            f"1bad://user:{SECRET}@host",  # bad scheme: used to hit "invalid scheme" first
            f"x://user:{SECRET}@[host",  # bad host: used to hit "not a valid URI" first
            f"x://user:{SECRET}@host/path",
        ],
    )
    def test_destination_parse(self, uri: str) -> None:
        with pytest.raises(ConfigurationError, match="username or password") as caught:
            Destination.parse(uri)
        assert self.SECRET not in self._full_text(caught.value)

    def test_at_sign_in_the_path_is_fine(self) -> None:
        assert Destination.parse("github://lab/repo@main/x").path == "/repo@main/x"

    def test_other_bad_values_are_not_echoed_either(self) -> None:
        with pytest.raises(ValidationError) as caught:
            Container(path=LogicalPath(("a",)), native_id=["token-abc123"])
        assert "token-abc123" not in self._full_text(caught.value)
        assert "native_id" in str(caught.value)
