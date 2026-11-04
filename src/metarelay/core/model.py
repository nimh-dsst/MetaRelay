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

__all__ = ["MetaRelayModel", "TranslatesValidationErrors", "describe_errors", "translate"]


def describe_errors(error: pydantic.ValidationError, what: str) -> str:
    """Pydantic's error as ``"field: problem; ..."``, without the input values.

    Pydantic's own message includes each bad input, which may be a token or a
    URI with a password in it. ``errors.py`` promises messages never contain
    credentials, so every MetaRelay message about a pydantic error is built
    here.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in detail['loc']) or what}: {detail['msg']}"
        for detail in error.errors(include_input=False, include_url=False)
    )


def translate(error: pydantic.ValidationError, what: str) -> ValidationError:
    """Convert pydantic's error into ours.

    A validator that raised our own ``ValidationError`` (a payload that does
    not fit its schema, say) arrives wrapped in pydantic's; surface the
    original message.

    Raise the result ``from None``: the pydantic error would carry the input
    values into tracebacks and logs.
    """
    for detail in error.errors():
        cause = (detail.get("ctx") or {}).get("error")
        if isinstance(cause, ValidationError):
            return cause
    return ValidationError(f"Invalid {what}: {describe_errors(error, what)}")


class TranslatesValidationErrors:
    """Mixin for pydantic models: raise ``metarelay.ValidationError`` instead."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        try:
            super().__init__(*args, **kwargs)
        except pydantic.ValidationError as error:
            raise translate(error, _what(type(self))) from None

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
            raise translate(error, _what(cls)) from None

    @classmethod
    def model_validate_json(cls, json_data: str | bytes | bytearray, **kwargs: Any) -> Self:
        try:
            return super().model_validate_json(json_data, **kwargs)  # type: ignore[misc]
        except pydantic.ValidationError as error:
            raise translate(error, _what(cls)) from None


def _what(cls: type) -> str:
    return cls.__name__


class MetaRelayModel(TranslatesValidationErrors, pydantic.BaseModel):
    """A frozen model that rejects unknown fields and raises only our errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)
