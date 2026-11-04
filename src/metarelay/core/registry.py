"""The schema registry: which payload model validates which kind of record.

A schema is a Pydantic model plus an ID and an integer version. MetaRelay's
own schemas (fiber photometry, microscopy) register themselves here, and so can
applications. Chronoxiv, for example, registers its workflow record schemas
without changing MetaRelay::

    from metarelay import register_schema

    register_schema("chronoxiv.implant", 3, ImplantPayload)

Versioning and migrations between versions are #8. This module only answers
"is this payload valid for schema X, version N?".
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, get_args

import pydantic
from pydantic_core import PydanticSerializationError

from metarelay.core.errors import ConfigurationError, ValidationError

__all__ = ["SchemaRegistry", "SchemaSpec", "default_registry", "register_schema"]

SCHEMA_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


@dataclass(frozen=True)
class SchemaSpec:
    """One registered schema version."""

    schema_id: str
    version: int
    model: type[pydantic.BaseModel]


class SchemaRegistry:
    """Maps ``(schema_id, version)`` to the model that validates the payload."""

    def __init__(self) -> None:
        self._specs: dict[tuple[str, int], SchemaSpec] = {}

    def register(self, schema_id: str, version: int, model: type[pydantic.BaseModel]) -> SchemaSpec:
        """Register ``model`` as the payload model for ``schema_id`` at ``version``.

        Registering the same model twice is a no-op. Registering a different
        model under an existing ID and version raises ``ConfigurationError``.

        The model, and every model nested in it, must:

        - set ``extra`` to ``"forbid"`` or ``"allow"``. Pydantic's default,
          ``"ignore"``, would accept unknown keys and then drop them from the
          stored payload without telling anyone.
        - read each field under the name it saves it under. Use
          ``Field(alias=...)``, which sets both; a ``validation_alias`` or
          ``serialization_alias`` on its own would save a record that can't
          be read back.
        """
        if not SCHEMA_ID_PATTERN.fullmatch(schema_id):
            raise ConfigurationError(
                f"Schema ID {schema_id!r} must be lowercase dotted names, "
                "for example 'fiber_photometry.implant'."
            )
        if version < 1:
            raise ConfigurationError(f"Schema version must be 1 or higher, not {version}.")
        _check_schema_model(model)
        key = (schema_id, version)
        existing = self._specs.get(key)
        if existing is not None:
            if existing.model is model:
                return existing
            raise ConfigurationError(
                f"Schema {schema_id} v{version} is already registered with "
                f"{existing.model.__qualname__}."
            )
        spec = SchemaSpec(schema_id, version, model)
        self._specs[key] = spec
        return spec

    def get(self, schema_id: str, version: int) -> SchemaSpec:
        """Return the spec, or raise ``ValidationError`` if it is not registered."""
        spec = self._specs.get((schema_id, version))
        if spec is None:
            raise ValidationError(f"Schema {schema_id} v{version} is not registered.")
        return spec

    def __contains__(self, key: object) -> bool:
        return key in self._specs

    def schemas(self) -> list[SchemaSpec]:
        """Every registered spec, sorted by ID and version."""
        return [self._specs[key] for key in sorted(self._specs)]

    def validate_payload(
        self, schema_id: str, version: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Validate ``payload`` and return it in normal form.

        Raises ``ValidationError`` unless ``payload`` is valid for the schema.

        The normal form is what the model understood, as JSON-ready data:

        - Values the model coerced keep the coerced type (``"3"`` becomes
          ``3`` for an ``int`` field), so ``{"count": "3"}`` and
          ``{"count": 3}`` get the same checksum.
        - Dates, enums and the like become their JSON form, so the checksum
          can always be computed.
        - Defaults are **not** added: a field the caller left out stays out,
          so adding an optional field to a schema doesn't change the checksum
          of existing records.
        - Keys are spelled the way the schema saves them (its aliases).

        The normal form is exactly what a backend saves as JSON, and reading
        it back gives the same normal form. A payload that can't make that
        round trip raises ``ValidationError`` here, at write time, instead of
        becoming a record nobody can read:

        - NaN or infinity anywhere in it (JSON can't store them, and pydantic
          would save them as ``null``);
        - a value the model saves in a form it can't read, or reads back as
          something different (a custom serializer, say).
        """
        model = self.get(schema_id, version).model
        try:
            validated = model.model_validate(payload)
        except pydantic.ValidationError as error:
            raise ValidationError(
                f"Payload is not valid for {schema_id} v{version}: {error}"
            ) from error
        what = f"Payload for {schema_id} v{version}"
        normal = _json_form(validated, what)
        if normal == payload:
            return normal  # already normal (a record read back): the round trip holds
        try:
            again = _json_form(model.model_validate(normal), what)
        except pydantic.ValidationError as error:
            raise ValidationError(f"{what} would not read back after saving: {error}") from error
        if again != normal:
            raise ValidationError(
                f"{what} reads back as something different after saving, so its checksum "
                "would change on every sync. Check the schema's custom serializers."
            )
        return normal


def _check_schema_model(model: type[pydantic.BaseModel]) -> None:
    """Refuse models whose payloads could not be saved and read back intact."""
    for each in _models_in(model, set()):
        name = each.__qualname__
        if each.model_config.get("extra") not in ("forbid", "allow"):
            raise ConfigurationError(
                f"Schema model {name} must set "
                'model_config = ConfigDict(extra="forbid") (or "allow"). '
                "With pydantic's default, unknown keys would be silently dropped."
            )
        by_alias = each.model_config.get("validate_by_alias", True) is not False
        for field_name, field in each.model_fields.items():
            saved_as = field.serialization_alias or field.alias or field_name
            if not by_alias:  # validate_by_alias=False ignores every alias
                read_as = field_name
            elif field.validation_alias is not None:
                read_as = field.validation_alias
            else:
                read_as = field.alias or field_name
            if read_as != saved_as:
                raise ConfigurationError(
                    f"Field {name}.{field_name} is read as {read_as!r} but saved as "
                    f"{saved_as!r}, so a saved record could not be read back. "
                    "Use Field(alias=...) to set both."
                )


def _json_form(validated: pydantic.BaseModel, what: str) -> dict[str, Any]:
    """The payload as a backend saves it: JSON data, without NaN or infinity."""
    try:
        # Check the Python values: the JSON dump already turns NaN into null.
        if _has_non_finite(validated.model_dump(by_alias=True, exclude_unset=True)):
            raise ValidationError(
                f"{what} contains NaN or infinity, which JSON can't store. "
                "Leave the field out, or use null, to mark a missing value."
            )
        return json.loads(validated.model_dump_json(by_alias=True, exclude_unset=True))
    except PydanticSerializationError as error:
        raise ValidationError(f"{what} can't be saved as JSON: {error}") from error


def _has_non_finite(value: Any) -> bool:
    if isinstance(value, Decimal):
        return not value.is_finite()
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_has_non_finite(item) for item in value.values())
    if isinstance(value, list | tuple | set | frozenset):
        return any(_has_non_finite(item) for item in value)
    return False


def _models_in(
    model: type[pydantic.BaseModel], seen: set[type]
) -> Iterator[type[pydantic.BaseModel]]:
    """``model`` and every model reachable through its field types."""
    if model in seen:
        return
    seen.add(model)
    yield model
    for field in model.model_fields.values():
        yield from _models_in_annotation(field.annotation, seen)


def _models_in_annotation(annotation: Any, seen: set[type]) -> Iterator[type[pydantic.BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        yield from _models_in(annotation, seen)
    for arg in get_args(annotation):
        yield from _models_in_annotation(arg, seen)


default_registry = SchemaRegistry()
"""The process-wide registry ``Record`` uses unless told otherwise."""


def register_schema(schema_id: str, version: int, model: type[pydantic.BaseModel]) -> SchemaSpec:
    """Register a schema in the default registry."""
    return default_registry.register(schema_id, version, model)
