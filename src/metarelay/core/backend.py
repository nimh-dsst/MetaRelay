"""Destination URIs, the ``Backend`` protocol, and ``connect()``.

A destination is a URI::

    file:///Users/me/metadata
    labarchives://api.labarchives.com
    elabftw://elab.example.org/api/v2
    github://my-lab/metadata@main/records

The scheme picks the backend. Backends register under the
``metarelay.backends`` entry-point group, in MetaRelay's own
``pyproject.toml`` or in a third-party package's::

    [project.entry-points."metarelay.backends"]
    mybackend = "my_package.backend:MyBackend"

``connect()`` imports only the backend it needs, so a missing extra
(``labapi`` for LabArchives, say) only matters when someone uses it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import Protocol, Self, runtime_checkable
from urllib.parse import urlsplit

import pydantic
from pydantic import ConfigDict

from metarelay.core.auth import AuthFlow, LoginChallenge
from metarelay.core.config import DestinationConfig
from metarelay.core.errors import ConfigurationError
from metarelay.core.session import Session

__all__ = [
    "ENTRY_POINT_GROUP",
    "Backend",
    "Destination",
    "auth_flow",
    "begin_login",
    "complete_login",
    "connect",
    "load_backend",
]

ENTRY_POINT_GROUP = "metarelay.backends"

# Backends that ship inside MetaRelay but need an extra installed.
_EXTRA_FOR_SCHEME = {"labarchives": "labarchives", "elabftw": "elabftw", "github": "github"}

_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*$")


class Destination(pydantic.BaseModel):
    """A parsed destination URI. Each backend interprets ``netloc`` and ``path``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str
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
        return cls(uri=uri, scheme=scheme, netloc=parts.netloc, path=parts.path)

    def __str__(self) -> str:
        return self.uri


@runtime_checkable
class Backend(Protocol):
    """What an entry point in ``metarelay.backends`` must provide.

    The entry point may name a class (instantiated with no arguments) or an
    instance.
    """

    name: str
    auth_flow: AuthFlow

    def open(self, destination: Destination, config: DestinationConfig | None) -> Session:
        """Open a session. Used by ``NONE`` and ``TOKEN`` backends."""
        ...

    def begin_login(
        self, destination: Destination, redirect_url: str, config: DestinationConfig | None
    ) -> LoginChallenge:
        """Start a redirect login. ``REDIRECT`` backends only."""
        ...

    def complete_login(
        self,
        challenge: LoginChallenge,
        params: Mapping[str, str],
        config: DestinationConfig | None,
    ) -> Session:
        """Finish a redirect login with the redirect's query parameters."""
        ...


def _entry_points() -> Iterable[EntryPoint]:
    return entry_points(group=ENTRY_POINT_GROUP)


def _install_hint(scheme: str) -> str:
    return f"Install it with: pip install 'metarelay[{_EXTRA_FOR_SCHEME[scheme]}]'"


def load_backend(scheme: str) -> Backend:
    """Find and import the backend registered for ``scheme``."""
    available = {ep.name: ep for ep in _entry_points()}
    entry_point = available.get(scheme)
    if entry_point is None:
        if scheme in _EXTRA_FOR_SCHEME:
            raise ConfigurationError(
                f"The {scheme}:// backend is not installed. {_install_hint(scheme)}"
            )
        installed = ", ".join(f"{name}://" for name in sorted(available)) or "none"
        raise ConfigurationError(
            f"No backend is installed for {scheme}://. Installed backends: {installed}."
        )
    try:
        loaded = entry_point.load()
    except ImportError as error:
        hint = _install_hint(scheme) if scheme in _EXTRA_FOR_SCHEME else str(error)
        raise ConfigurationError(f"The {scheme}:// backend could not load. {hint}") from error
    backend = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(backend, Backend):
        raise ConfigurationError(
            f"The {scheme}:// entry point ({entry_point.value}) is not a MetaRelay backend."
        )
    return backend


def auth_flow(uri: str) -> AuthFlow:
    """Which login flow ``uri`` needs, so a UI knows what to show."""
    return load_backend(Destination.parse(uri).scheme).auth_flow


def connect(uri: str, config: DestinationConfig | None = None) -> Session:
    """Open a session to ``uri``.

    For a destination that needs a browser login (``AuthFlow.REDIRECT``), use
    ``begin_login`` and ``complete_login`` instead.
    """
    destination = Destination.parse(uri)
    backend = load_backend(destination.scheme)
    if backend.auth_flow is AuthFlow.REDIRECT:
        raise ConfigurationError(
            f"{destination.scheme}:// needs a browser login. "
            "Use begin_login() and complete_login() instead of connect()."
        )
    return backend.open(destination, config)


def begin_login(
    uri: str, redirect_url: str, config: DestinationConfig | None = None
) -> LoginChallenge:
    """Start a redirect login. Send the user to ``challenge.auth_url``."""
    destination = Destination.parse(uri)
    backend = load_backend(destination.scheme)
    if backend.auth_flow is not AuthFlow.REDIRECT:
        raise ConfigurationError(
            f"{destination.scheme}:// does not use a browser login. Use connect() instead."
        )
    return backend.begin_login(destination, redirect_url, config)


def complete_login(
    challenge: LoginChallenge,
    params: Mapping[str, str],
    config: DestinationConfig | None = None,
) -> Session:
    """Finish a redirect login with the query parameters from the redirect."""
    destination = Destination.parse(challenge.destination)
    backend = load_backend(destination.scheme)
    if backend.auth_flow is not AuthFlow.REDIRECT:
        raise ConfigurationError(f"{destination.scheme}:// does not use a browser login.")
    return backend.complete_login(challenge, params, config)
