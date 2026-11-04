from typing import Any

import pydantic
import pytest

from metarelay import ConfigurationError, ValidationError
from metarelay.core.registry import SchemaRegistry


class Payload(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    name: str


class OtherPayload(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

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


class Loose(pydantic.BaseModel):
    """Pydantic's default: unknown keys are accepted, then dropped."""

    name: str


class HoldsLoose(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    items: list[Loose | None] = []


class Allows(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")

    name: str


@pytest.mark.parametrize("model", [Loose, HoldsLoose], ids=["top-level", "nested"])
def test_schemas_that_would_silently_drop_keys_are_rejected(model) -> None:
    with pytest.raises(ConfigurationError, match="Loose must set"):
        SchemaRegistry().register("app.thing", 1, model)


def test_unknown_keys_are_kept_or_rejected_never_dropped() -> None:
    registry = SchemaRegistry()
    registry.register("app.forbid", 1, Payload)
    registry.register("app.allow", 1, Allows)
    with pytest.raises(ValidationError):
        registry.validate_payload("app.forbid", 1, {"name": "x", "notes": "kept?"})
    assert registry.validate_payload("app.allow", 1, {"name": "x", "notes": "kept"}) == {
        "name": "x",
        "notes": "kept",
    }


class TestRoundTrip:
    """Normal form must validate again when it is read back (review on #17)."""

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_numbers_are_refused_anywhere(self, bad: float) -> None:
        class Inner(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")

            v: float

        class Free(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="allow")

            d: float | None = None
            inner: Inner | None = None
            anything: dict[str, Any] | None = None

        registry = SchemaRegistry()
        registry.register("app.free", 1, Free)
        # A typed field, a nested model, an Any field, and an extra key.
        for payload in ({"d": bad}, {"inner": {"v": bad}}, {"anything": {"v": bad}}, {"x": bad}):
            with pytest.raises(ValidationError, match="NaN or infinity"):
                registry.validate_payload("app.free", 1, payload)

    def test_unserializable_extra_is_a_validation_error(self) -> None:
        class Free(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="allow")

        registry = SchemaRegistry()
        registry.register("app.free", 1, Free)
        with pytest.raises(ValidationError, match="can't be saved as JSON"):
            registry.validate_payload("app.free", 1, {"x": object()})

    def test_validation_only_alias_is_refused(self) -> None:
        class Aliased(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")

            count: int = pydantic.Field(validation_alias="Count")

        with pytest.raises(ConfigurationError, match=r"read as 'Count' but saved as 'count'"):
            SchemaRegistry().register("app.aliased", 1, Aliased)

    def test_serialization_only_alias_is_refused(self) -> None:
        class Aliased(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")

            count: int = pydantic.Field(serialization_alias="Count")

        with pytest.raises(ConfigurationError, match=r"read as 'count' but saved as 'Count'"):
            SchemaRegistry().register("app.aliased", 1, Aliased)

    def test_alias_not_used_for_reading_is_refused(self) -> None:
        class Aliased(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(
                extra="forbid", validate_by_alias=False, validate_by_name=True
            )

            count: int = pydantic.Field(alias="Count")

        with pytest.raises(ConfigurationError, match="read as 'count'"):
            SchemaRegistry().register("app.aliased", 1, Aliased)

    def test_alias_choices_are_refused(self) -> None:
        class Choices(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")

            count: int = pydantic.Field(validation_alias=pydantic.AliasChoices("Count", "n"))

        with pytest.raises(ConfigurationError, match="Field"):
            SchemaRegistry().register("app.choices", 1, Choices)

    def test_plain_alias_round_trips(self) -> None:
        class Aliased(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid", populate_by_name=True)

            count: int = pydantic.Field(alias="Count")

        registry = SchemaRegistry()
        registry.register("app.alias", 1, Aliased)
        # Either spelling is accepted; the saved spelling is the alias.
        for given in ({"Count": 1}, {"count": 1}):
            normal = registry.validate_payload("app.alias", 1, given)
            assert normal == {"Count": 1}
            assert registry.validate_payload("app.alias", 1, normal) == normal

    def test_payload_that_reads_back_differently_is_refused(self) -> None:
        class Drifts(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")

            n: int

            @pydantic.field_serializer("n")
            def _bump(self, value: int) -> int:
                return value + 1

        registry = SchemaRegistry()
        registry.register("app.drifts", 1, Drifts)
        with pytest.raises(ValidationError, match="reads back as something different"):
            registry.validate_payload("app.drifts", 1, {"n": 1})

    def test_payload_that_would_not_read_back_is_refused_at_write(self) -> None:
        class Lossy(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")

            code: int

            @pydantic.field_serializer("code")
            def _as_text(self, value: int) -> str:
                return f"#{value}"

        registry = SchemaRegistry()
        registry.register("app.lossy", 1, Lossy)
        with pytest.raises(ValidationError, match="would not read back"):
            registry.validate_payload("app.lossy", 1, {"code": 7})
