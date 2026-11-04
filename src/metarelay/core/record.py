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
created, and stored in normal form (see ``SchemaRegistry.validate_payload``):
coerced, JSON-ready, without added defaults. Any failure, in the envelope or
the payload, raises ``metarelay.ValidationError``.

Records are frozen, but ``payload`` and ``context`` are ordinary dicts, so
Python can't stop ``record.payload["x"] = ...``. Treat them as read-only:
an edit made that way skips validation. To change a record, validate a new
one: ``Record.model_validate({**record.model_dump(), "payload": new_payload})``.
(``model_copy(update=...)`` skips validation too.)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import Field, ValidationInfo, field_validator

from metarelay.core.model import MetaRelayModel
from metarelay.core.registry import SCHEMA_ID_PATTERN, SchemaRegistry, default_registry

__all__ = ["Record"]

# Same rule as Chronoxiv: safe as a filename and in a URL on every backend.
RECORD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _now() -> datetime:
    return datetime.now().astimezone()


class Record(MetaRelayModel):
    """A validated metadata record.

    To validate against a registry other than the default one, pass it in the
    validation context::

        Record.model_validate(data, context={"registry": my_registry})
    """

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

    @field_validator("payload")
    @classmethod
    def _check_payload(cls, payload: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        # schema_id and schema_version are declared first, so they are already
        # validated here. If either failed, that error is reported instead.
        if "schema_id" not in info.data or "schema_version" not in info.data:
            return payload
        registry: SchemaRegistry = (info.context or {}).get("registry", default_registry)
        return registry.validate_payload(
            info.data["schema_id"], info.data["schema_version"], payload
        )
