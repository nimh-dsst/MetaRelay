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

__all__ = [
    "AuthenticationError",
    "BackendUnavailableError",
    "ConfigurationError",
    "ConflictError",
    "MetaRelayError",
    "NotFoundError",
    "PermissionDeniedError",
    "ValidationError",
]
