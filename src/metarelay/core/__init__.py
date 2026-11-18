"""MetaRelay's backend-neutral public API."""

from metarelay.core.auth import AuthFlow, LoginChallenge
from metarelay.core.backend import (
    Backend,
    Destination,
    auth_flow,
    begin_login,
    complete_login,
    connect,
)
from metarelay.core.checksum import record_checksum
from metarelay.core.config import DestinationConfig, TokenConfig
from metarelay.core.conflict import WriteAction, WriteMode, accept_edits, decide_write
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
from metarelay.core.preview import (
    Heading,
    Preview,
    PreviewDiff,
    PreviewEdit,
    Table,
    apply_edits,
    register_renderer,
    render_preview,
)
from metarelay.core.receipt import FileReceipt, Receipt, SyncIndex
from metarelay.core.record import Record
from metarelay.core.registry import SchemaRegistry, SchemaSpec, register_schema
from metarelay.core.session import Session, StoredRecord

__all__ = [
    "ROOT",
    "AuthFlow",
    "AuthenticationError",
    "Backend",
    "BackendUnavailableError",
    "ConfigurationError",
    "ConflictError",
    "Container",
    "Destination",
    "DestinationConfig",
    "FileReceipt",
    "Heading",
    "LogicalPath",
    "LoginChallenge",
    "MetaRelayError",
    "NotFoundError",
    "PermissionDeniedError",
    "Preview",
    "PreviewDiff",
    "PreviewEdit",
    "Receipt",
    "Record",
    "SchemaRegistry",
    "SchemaSpec",
    "Session",
    "StoredRecord",
    "SyncIndex",
    "Table",
    "TokenConfig",
    "ValidationError",
    "WriteAction",
    "WriteMode",
    "accept_edits",
    "apply_edits",
    "auth_flow",
    "begin_login",
    "complete_login",
    "connect",
    "decide_write",
    "record_checksum",
    "register_renderer",
    "register_schema",
    "render_preview",
]
