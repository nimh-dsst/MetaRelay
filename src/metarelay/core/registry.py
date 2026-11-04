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

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pydantic

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
        """
        if not SCHEMA_ID_PATTERN.fullmatch(schema_id):
            raise ConfigurationError(
                f"Schema ID {schema_id!r} must be lowercase dotted names, "
                "for example 'fiber_photometry.implant'."
            )
        if version < 1:
            raise ConfigurationError(f"Schema version must be 1 or higher, not {version}.")
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
        - Keys keep the caller's spelling (aliases are kept).
        """
        model = self.get(schema_id, version).model
        try:
            validated = model.model_validate(payload)
        except pydantic.ValidationError as error:
            raise ValidationError(
                f"Payload is not valid for {schema_id} v{version}: {error}"
            ) from error
        return validated.model_dump(mode="json", by_alias=True, exclude_unset=True)


default_registry = SchemaRegistry()
"""The process-wide registry ``Record`` uses unless told otherwise."""


def register_schema(schema_id: str, version: int, model: type[pydantic.BaseModel]) -> SchemaSpec:
    """Register a schema in the default registry."""
    return default_registry.register(schema_id, version, model)
