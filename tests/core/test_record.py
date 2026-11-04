import pydantic
import pytest

from metarelay import Record, ValidationError
from metarelay.core.registry import SchemaRegistry


def test_valid_record(make_record) -> None:
    record = make_record()
    assert record.schema_id == "test.fiber"
    assert record.payload == {"diameter_um": 400.0}
    assert record.updated_at.tzinfo is not None


def test_payload_is_validated_against_the_registry(make_record) -> None:
    with pytest.raises(ValidationError, match=r"not valid for test\.fiber v1"):
        make_record(payload={"diameter_um": "wide"})


def test_unregistered_schema_is_rejected(make_record) -> None:
    with pytest.raises(ValidationError, match="not registered"):
        make_record(schema_id="test.unknown")


def test_payload_is_kept_exactly_as_given(make_record) -> None:
    # The model has a default for numerical_aperture, but the record must not
    # gain it: adding fields would change the checksum.
    assert "numerical_aperture" not in make_record().payload


@pytest.mark.parametrize("record_id", ["", ".", "..", "-leading", "has space", "a/b"])
def test_bad_record_ids_are_rejected(make_record, record_id: str) -> None:
    with pytest.raises(ValidationError):
        make_record(record_id=record_id)


@pytest.mark.parametrize("record_id", ["implant-M1", "equipment", "a.b_c-1"])
def test_good_record_ids(make_record, record_id: str) -> None:
    assert make_record(record_id=record_id).record_id == record_id


def test_unknown_envelope_fields_are_rejected(make_record) -> None:
    with pytest.raises(ValidationError):
        make_record(record_type="implant")


def test_records_are_immutable(make_record) -> None:
    record = make_record()
    with pytest.raises(pydantic.ValidationError):
        record.record_id = "other"  # type: ignore[misc]


def test_every_failure_is_a_metarelay_validation_error() -> None:
    with pytest.raises(ValidationError):
        Record.model_validate({"schema_id": "test.fiber"})
    with pytest.raises(ValidationError):
        Record.model_validate_json("{}")


def test_round_trips_through_json(make_record) -> None:
    record = make_record()
    assert Record.model_validate_json(record.model_dump_json()) == record


def test_a_custom_registry_can_be_passed_in_context(make_record) -> None:
    other = SchemaRegistry()
    data = make_record().model_dump()
    with pytest.raises(ValidationError, match="not registered"):
        Record.model_validate(data, context={"registry": other})
