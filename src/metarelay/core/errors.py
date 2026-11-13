"""The MetaRelay error hierarchy.

Every backend translates its native exceptions (``labapi`` errors, HTTP
errors, ``OSError``) into one of these classes, so callers handle failures the
same way for every destination::

    try:
        session.write_record(path, record)
    except ConflictError:
        ...  # ask the user whether to keep the remote copy or overwrite it
    except MetaRelayError:
        ...  # anything else MetaRelay raised

Messages are shown to users. Never put a request URL, token, or other
credential in a message: ``labapi`` signs requests in the query string, so
``str()`` of a native HTTP error can leak secrets. Chain the native error
(``raise ... from error``) only when it can't hold secrets: a chained
``__cause__`` is printed in tracebacks and logs. Otherwise use ``from None``.
"""

from __future__ import annotations

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


class MetaRelayError(Exception):
    """Base class for every error MetaRelay raises on purpose."""


class ConfigurationError(MetaRelayError):
    """The destination, config object, or installed extras are wrong.

    Examples: an unknown URI scheme, a missing required setting, or a backend
    whose extra is not installed.
    """


class AuthenticationError(MetaRelayError):
    """The destination rejected the credentials, or the login did not finish."""


class BackendUnavailableError(MetaRelayError):
    """The destination could not be reached or returned a server error.

    Usually temporary: a network failure, a timeout, or a 5xx response. Also
    raised for an error the destination reports that MetaRelay can't classify
    more precisely.
    """


class NotFoundError(MetaRelayError):
    """A container, record, or file does not exist at the destination."""


class ConflictError(MetaRelayError):
    """A write would overwrite changes MetaRelay did not make.

    Raised when the remote record changed since the last receipt, when a
    different record already uses the same ID, or when ``create`` mode finds an
    existing record.

    Also raised, by reads as well as writes, when a name at the destination is
    ambiguous (two LabArchives folders with the same name, for example), since
    MetaRelay can't tell which one is meant. Renaming one at the destination
    fixes it.
    """


class ValidationError(MetaRelayError, ValueError):
    """A record, payload, or path failed validation.

    Also a ``ValueError``, so code that already catches ``ValueError`` keeps
    working.
    """


class PermissionDeniedError(MetaRelayError):
    """The credentials are valid but not allowed to do this."""
