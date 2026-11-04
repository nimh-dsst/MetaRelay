"""Destination URIs.

A destination is a URI::

    file:///Users/me/metadata
    labarchives://api.labarchives.com
    elabftw://elab.example.org/api/v2
    github://my-lab/metadata@main/records

The scheme picks the backend; each backend interprets ``netloc`` and ``path``.

URIs are normalized so the same destination always has the same spelling:
the scheme and host are lowercased and a trailing ``/`` is dropped.
Receipts are keyed by destination, so without this ``LabArchives://host/``
and ``labarchives://host`` would look like two destinations, the receipt
would not be found, and the next write would be a spurious conflict.
"""

from __future__ import annotations

import re
from typing import Self
from urllib.parse import urlsplit, urlunsplit

from metarelay.core.errors import ConfigurationError
from metarelay.core.model import MetaRelayModel

__all__ = ["Destination", "normalize_destination", "validate_destination"]

_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*$")


class Destination(MetaRelayModel):
    """A parsed, normalized destination URI."""

    uri: str
    """The normalized URI. Receipts store this form."""
    scheme: str
    netloc: str
    path: str

    @classmethod
    def parse(cls, uri: str) -> Self:
        if "://" not in uri:
            raise ConfigurationError(
                f"Destination {uri!r} is not a URI. Use a form like file:///path/to/folder."
            )
        parts = urlsplit(uri)
        scheme = parts.scheme.lower()
        if not _SCHEME_PATTERN.fullmatch(scheme):
            raise ConfigurationError(f"Destination {uri!r} has an invalid scheme.")
        if "@" in parts.netloc:
            # Receipts store the URI, so a password in it would be saved to disk.
            raise ConfigurationError(
                "Destination URIs cannot contain a username or password. "
                "Pass credentials in a config object instead."
            )
        netloc = parts.netloc.lower()
        path = parts.path.rstrip("/")
        if scheme == "file" and not path:
            path = "/"  # file:/// is the filesystem root, not "no path"
        normalized = urlunsplit((scheme, netloc, path, parts.query, parts.fragment))
        return cls(uri=normalized, scheme=scheme, netloc=netloc, path=path)

    def __str__(self) -> str:
        return self.uri


def normalize_destination(uri: str) -> str:
    """The canonical spelling of ``uri``. Raises ``ConfigurationError``."""
    return Destination.parse(uri).uri


def validate_destination(uri: str) -> str:
    """``normalize_destination`` for use in a model's field validator.

    A bad URI inside a stored receipt is invalid data, so it raises
    ``ValueError``, which the model reports as ``metarelay.ValidationError``.
    """
    try:
        return normalize_destination(uri)
    except ConfigurationError as error:
        raise ValueError(str(error)) from error
