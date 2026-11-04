"""MetaRelay's backend-neutral public API."""

from metarelay.core.auth import AuthFlow, LoginChallenge
from metarelay.core.checksum import record_checksum
from metarelay.core.config import DestinationConfig, TokenConfig
from metarelay.core.conflict import WriteAction, WriteMode, decide_write
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
from metarelay.core.session import Session, StoredRecord

__all__ = [
    "ROOT",
    "AuthFlow",
    "AuthenticationError",
    "BackendUnavailableError",
    "ConfigurationError",
    "ConflictError",
    "Container",
    "DestinationConfig",
    "FileReceipt",
    "LogicalPath",
    "LoginChallenge",
    "MetaRelayError",
    "NotFoundError",
    "PermissionDeniedError",
    "Receipt",
    "Record",
    "SchemaRegistry",
    "SchemaSpec",
    "Session",
    "StoredRecord",
    "SyncIndex",
    "TokenConfig",
    "ValidationError",
    "WriteAction",
    "WriteMode",
    "decide_write",
    "record_checksum",
    "register_schema",
]
