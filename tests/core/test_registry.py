import pydantic
import pytest

from metarelay import ConfigurationError, ValidationError
from metarelay.core.registry import SchemaRegistry


class Payload(pydantic.BaseModel):
    name: str


class OtherPayload(pydantic.BaseModel):
    name: str


def test_register_and_get() -> None:
    registry = SchemaRegistry()
    spec = registry.register("app.thing", 2, Payload)
    assert registry.get("app.thing", 2) is spec
    assert ("app.thing", 2) in registry
    assert registry.schemas() == [spec]


def test_registering_the_same_model_twice_is_a_no_op() -> None:
    registry = SchemaRegistry()
    first = registry.register("app.thing", 1, Payload)
    assert registry.register("app.thing", 1, Payload) is first


def test_registering_a_different_model_under_the_same_version_fails() -> None:
    registry = SchemaRegistry()
    registry.register("app.thing", 1, Payload)
    with pytest.raises(ConfigurationError, match="already registered"):
        registry.register("app.thing", 1, OtherPayload)


def test_versions_are_independent() -> None:
    registry = SchemaRegistry()
    registry.register("app.thing", 1, Payload)
    registry.register("app.thing", 2, OtherPayload)
    assert registry.get("app.thing", 2).model is OtherPayload


@pytest.mark.parametrize("schema_id", ["App.Thing", "app thing", "1app", "app.", ""])
def test_schema_id_format_is_enforced(schema_id: str) -> None:
    with pytest.raises(ConfigurationError):
        SchemaRegistry().register(schema_id, 1, Payload)


def test_version_must_be_positive() -> None:
    with pytest.raises(ConfigurationError):
        SchemaRegistry().register("app.thing", 0, Payload)


def test_unregistered_schema_is_a_validation_error() -> None:
    with pytest.raises(ValidationError, match="not registered"):
        SchemaRegistry().get("app.missing", 1)


def test_validate_payload() -> None:
    registry = SchemaRegistry()
    registry.register("app.thing", 1, Payload)
    registry.validate_payload("app.thing", 1, {"name": "ok"})
    with pytest.raises(ValidationError, match=r"not valid for app\.thing v1"):
        registry.validate_payload("app.thing", 1, {"name": 5})
