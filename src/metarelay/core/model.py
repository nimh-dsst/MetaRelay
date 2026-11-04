"""The base class for every public MetaRelay model.

Pydantic raises its own ``pydantic.ValidationError``. MetaRelay promises that
callers only ever have to catch ``metarelay`` errors, so every public model
converts pydantic's error into ``metarelay.ValidationError`` here, in one
place, for construction, ``model_validate`` and ``model_validate_json``.

Because the parent model translates, a bad nested value (a bad path inside a
``Receipt`` loaded from JSON, say) is covered too.
"""

from __future__ import annotations

from typing import Any, Self

import pydantic
from pydantic import ConfigDict

from metarelay.core.errors import ValidationError

__all__ = ["MetaRelayModel", "TranslatesValidationErrors", "translate"]


def translate(error: pydantic.ValidationError, what: str) -> ValidationError:
    """Convert pydantic's error into ours.

    A validator that raised our own ``ValidationError`` (a payload that does
    not fit its schema, say) arrives wrapped in pydantic's; surface the
    original message.
    """
    for detail in error.errors():
        cause = (detail.get("ctx") or {}).get("error")
        if isinstance(cause, ValidationError):
            return cause
    return ValidationError(f"Invalid {what}: {error}")


class TranslatesValidationErrors:
    """Mixin for pydantic models: raise ``metarelay.ValidationError`` instead."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        try:
            super().__init__(*args, **kwargs)
        except pydantic.ValidationError as error:
            raise translate(error, _what(type(self))) from error

    # Tell pydantic this __init__ only wraps errors. Without the marker,
    # pydantic treats it as a custom __init__ and routes model_validate through
    # it, which drops the validation context (and so Record's registry).
    # Pinned by test_a_custom_registry_can_be_passed_in_context.
    __init__.__pydantic_base_init__ = True  # type: ignore[attr-defined]

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate(obj, **kwargs)  # type: ignore[misc]
        except pydantic.ValidationError as error:
            raise translate(error, _what(cls)) from error

    @classmethod
    def model_validate_json(cls, json_data: str | bytes | bytearray, **kwargs: Any) -> Self:
        try:
            return super().model_validate_json(json_data, **kwargs)  # type: ignore[misc]
        except pydantic.ValidationError as error:
            raise translate(error, _what(cls)) from error


def _what(cls: type) -> str:
    return cls.__name__


class MetaRelayModel(TranslatesValidationErrors, pydantic.BaseModel):
    """A frozen model that rejects unknown fields and raises only our errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)
