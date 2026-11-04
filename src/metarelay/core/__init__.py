"""MetaRelay's backend-neutral public API."""

from metarelay.core.errors import (
    AuthenticationError,
    BackendUnavailableError,
    ConfigurationError,
    ConflictError,
    MetaRelayError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from metarelay.core.record import Record
from metarelay.core.registry import SchemaRegistry, SchemaSpec, register_schema

__all__ = [
    "AuthenticationError",
    "BackendUnavailableError",
    "ConfigurationError",
    "ConflictError",
    "MetaRelayError",
    "NotFoundError",
    "PermissionDeniedError",
    "Record",
    "SchemaRegistry",
    "SchemaSpec",
    "ValidationError",
    "register_schema",
]
