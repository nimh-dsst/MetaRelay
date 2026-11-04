"""MetaRelay's backend-neutral public API."""

from metarelay.core.checksum import record_checksum
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
from metarelay.core.path import ROOT, Container, LogicalPath
from metarelay.core.receipt import FileReceipt, Receipt, SyncIndex
from metarelay.core.record import Record
from metarelay.core.registry import SchemaRegistry, SchemaSpec, register_schema

__all__ = [
    "ROOT",
    "AuthenticationError",
    "BackendUnavailableError",
    "ConfigurationError",
    "ConflictError",
    "Container",
    "FileReceipt",
    "LogicalPath",
    "MetaRelayError",
    "NotFoundError",
    "PermissionDeniedError",
    "Receipt",
    "Record",
    "SchemaRegistry",
    "SchemaSpec",
    "SyncIndex",
    "ValidationError",
    "record_checksum",
    "register_schema",
]
