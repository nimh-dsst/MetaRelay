import datetime as dt
import enum

import pydantic
import pytest

from metarelay import Record, ValidationError, record_checksum
from metarelay.core.registry import SchemaRegistry


def test_valid_record(make_record) -> None:
    record = make_record()
    assert record.schema_id == "test.fiber"
    assert record.payload == {"diameter_um": 400.0}
    assert record.updated_at.tzinfo is not None


def test_payload_is_validated_against_the_registry(make_record) -> None:
    with pytest.raises(ValidationError, match=r"not valid for test\.fiber v1"):
        make_record(payload={"diameter_um": "wide"})


def test_defaults_are_not_added_to_the_payload(make_record) -> None:
    # The model has a default for numerical_aperture, but the record must not
    # gain it: adding an optional field to a schema would change every checksum.
    assert "numerical_aperture" not in make_record().payload


class Site(enum.StrEnum):
    NAC = "NAc"


class Rich(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    count: int
    when: dt.date | None = None
    site: Site | None = None
    label: str | None = pydantic.Field(default=None, alias="Label")


@pytest.fixture
def rich(isolated_registry):
    isolated_registry.register("test.rich", 1, Rich)

    def _make(payload: dict) -> Record:
        return Record(schema_id="test.rich", schema_version=1, record_id="r", payload=payload)

    return _make


def test_coerced_values_get_the_same_checksum(rich) -> None:
    # Pydantic accepts "3" for an int. Storing "3" would make the same
    # metadata look changed: a spurious conflict.
    assert rich({"count": "3"}).payload == {"count": 3}
    assert record_checksum(rich({"count": "3"})) == record_checksum(rich({"count": 3}))


def test_non_json_values_are_stored_as_json(rich) -> None:
    record = rich({"count": 1, "when": dt.date(2026, 11, 4), "site": Site.NAC})
    assert record.payload == {"count": 1, "when": "2026-11-04", "site": "NAc"}
    record_checksum(record)  # used to raise TypeError
    assert Record.model_validate_json(record.model_dump_json()) == record


def test_payload_keys_keep_their_aliases(rich) -> None:
    assert rich({"count": 1, "Label": "x"}).payload == {"count": 1, "Label": "x"}


@pytest.mark.parametrize("record_id", ["", "..", "a/b"], ids=["empty", "first-char", "later-char"])
def test_bad_record_ids_are_rejected(make_record, record_id: str) -> None:
    with pytest.raises(ValidationError):
        make_record(record_id=record_id)


def test_good_record_id(make_record) -> None:
    assert make_record(record_id="a.b_c-1").record_id == "a.b_c-1"


def test_unknown_envelope_fields_are_rejected(make_record) -> None:
    with pytest.raises(ValidationError):
        make_record(record_type="implant")


def test_a_custom_registry_can_be_passed_in_context(make_record) -> None:
    other = SchemaRegistry()
    data = make_record().model_dump()
    with pytest.raises(ValidationError, match="not registered"):
        Record.model_validate(data, context={"registry": other})


def test_record_round_trips_through_dump_and_json(rich) -> None:
    # Johnny's repro on #17: Record.model_validate(record.model_dump()) must work,
    # because it is the edit recipe and what every backend's read_record does.
    record = Record(
        schema_id="test.rich",
        schema_version=1,
        record_id="r1",
        payload={"count": "3", "Label": "x", "when": "2026-01-02", "site": "NAc"},
    )
    assert Record.model_validate(record.model_dump()) == record
    assert Record.model_validate_json(record.model_dump_json()) == record
