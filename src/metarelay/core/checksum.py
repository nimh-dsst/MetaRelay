"""Content fingerprints for conflict detection.

The recipe is Chronoxiv's: SHA-256 over JSON with sorted keys and no
whitespace, so the same data always gives the same checksum no matter how it
was formatted on disk.

The recipe matches, but the fields hashed do not (``schema_id`` replaces
Chronoxiv's ``record_type``, and the NDX version is no longer in the
envelope). Checksums in existing Chronoxiv receipts therefore do not match
these, and migrated receipts need a verify-then-upgrade step.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from metarelay.core.record import Record

__all__ = ["canonical_json", "preview_checksum", "record_checksum"]


def canonical_json(value: Mapping[str, Any]) -> bytes:
    """Serialize ``value`` the same way every time."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def record_checksum(record: Record) -> str:
    """Fingerprint a record's scientific content.

    ``updated_at`` is left out, as in Chronoxiv: saving the same metadata again
    must not look like a change.
    """
    content = {
        "schema_id": record.schema_id,
        "schema_version": record.schema_version,
        "record_id": record.record_id,
        "context": record.context,
        "payload": record.payload,
    }
    return hashlib.sha256(canonical_json(content)).hexdigest()


def preview_checksum(rendered: str) -> str:
    """Fingerprint a rendered preview, as stored at the destination (D7)."""
    return hashlib.sha256(rendered.encode()).hexdigest()
