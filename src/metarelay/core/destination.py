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
from urllib.parse import urlsplit

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
        # First, before any message can echo the URI: receipts store it, and
        # errors are shown and logged, so a password must go nowhere.
        if "@" in _authority(uri):
            raise ConfigurationError(
                "Destination URIs cannot contain a username or password. "
                "Pass credentials in a config object instead."
            )
        if "://" not in uri:
            raise ConfigurationError(
                f"Destination {uri!r} is not a URI. Use a form like file:///path/to/folder."
            )
        try:
            parts = urlsplit(uri)
        except ValueError as error:  # e.g. "Invalid IPv6 URL" for "x://[host"
            raise ConfigurationError(f"Destination {uri!r} is not a valid URI: {error}.") from error
        scheme = parts.scheme.lower()
        if not _SCHEME_PATTERN.fullmatch(scheme):
            raise ConfigurationError(f"Destination {uri!r} has an invalid scheme.")
        netloc = parts.netloc.lower()
        path = parts.path.rstrip("/")
        if scheme == "file" and not path:
            path = "/"  # file:/// is the filesystem root, not "no path"
        # Built by hand: urlunsplit drops the "//" for an empty host on schemes
        # it doesn't know, and parse() would then reject its own output.
        normalized = f"{scheme}://{netloc}{path}"
        if parts.query:
            normalized += f"?{parts.query}"
        if parts.fragment:
            normalized += f"#{parts.fragment}"
        return cls(uri=normalized, scheme=scheme, netloc=netloc, path=path)

    def __str__(self) -> str:
        return self.uri


def _authority(uri: str) -> str:
    """The ``user:password@host`` part, found without parsing (parsing can fail).

    Also covers text with no ``://``, like ``user:pw@host``. An ``@`` later
    in the path (``github://lab/repo@main``) is not part of it.
    """
    after_scheme = uri.split("://", 1)[-1]
    return re.split(r"[/?#]", after_scheme, maxsplit=1)[0]


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
