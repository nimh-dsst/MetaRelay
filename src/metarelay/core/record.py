"""``Record``: the unit MetaRelay moves between destinations.

A record is an envelope around a payload::

    Record(
        schema_id="fiber_photometry.implant",   # which schema the payload follows
        schema_version=1,
        record_id="implant-M1",                 # unique within its container
        context={"cohort": "A", "subject_id": "M1"},
        payload={...},                          # the metadata itself
    )

The envelope is lifted from Chronoxiv's ``PortableMetadataRecord``, with
``record_type`` generalized to ``schema_id``/``schema_version`` so any
registered schema can travel in it.

The payload is validated against the schema registry when the record is
created. Any failure, in the envelope or the payload, raises
``metarelay.ValidationError``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Self

import pydantic
from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from metarelay.core.errors import ValidationError
from metarelay.core.registry import SCHEMA_ID_PATTERN, SchemaRegistry, default_registry

__all__ = ["Record"]

# Same rule as Chronoxiv: safe as a filename and in a URL on every backend.
RECORD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _now() -> datetime:
    return datetime.now().astimezone()


class Record(pydantic.BaseModel):
    """A validated metadata record.

    To validate against a registry other than the default one, pass it in the
    validation context::

        Record.model_validate(data, context={"registry": my_registry})
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: str
    schema_version: int = Field(ge=1)
    record_id: str
    context: dict[str, str | None] = Field(default_factory=dict)
    payload: dict[str, Any]
    updated_at: datetime = Field(default_factory=_now)
    """When the record was last edited. Not part of the checksum."""

    @field_validator("schema_id")
    @classmethod
    def _check_schema_id(cls, value: str) -> str:
        if not SCHEMA_ID_PATTERN.fullmatch(value):
            raise ValueError("must be lowercase dotted names, like 'fiber_photometry.implant'")
        return value

    @field_validator("record_id")
    @classmethod
    def _check_record_id(cls, value: str) -> str:
        if not RECORD_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "must start with a letter or number and contain only letters, numbers, "
                "periods, underscores, or hyphens"
            )
        return value

    @model_validator(mode="after")
    def _check_payload(self, info: ValidationInfo) -> Self:
        registry: SchemaRegistry = (info.context or {}).get("registry", default_registry)
        registry.validate_payload(self.schema_id, self.schema_version, self.payload)
        return self

    # Pydantic wraps errors raised inside validators in its own
    # ValidationError. Convert them back so callers only ever see ours.

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except pydantic.ValidationError as error:
            raise _translate(error) from error

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate(obj, **kwargs)
        except pydantic.ValidationError as error:
            raise _translate(error) from error

    @classmethod
    def model_validate_json(cls, json_data: str | bytes | bytearray, **kwargs: Any) -> Self:
        try:
            return super().model_validate_json(json_data, **kwargs)
        except pydantic.ValidationError as error:
            raise _translate(error) from error


def _translate(error: pydantic.ValidationError) -> ValidationError:
    # A payload or registry failure arrives as our own ValidationError wrapped
    # in pydantic's; surface the original message.
    for detail in error.errors():
        cause = (detail.get("ctx") or {}).get("error")
        if isinstance(cause, ValidationError):
            return cause
    return ValidationError(f"Invalid record: {error}")
