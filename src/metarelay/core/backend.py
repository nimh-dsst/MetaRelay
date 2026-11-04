"""The ``Backend`` protocol and ``connect()``.

The scheme of a destination URI (see ``metarelay.core.destination``) picks
the backend. Backends register under the
``metarelay.backends`` entry-point group, in MetaRelay's own
``pyproject.toml`` or in a third-party package's::

    [project.entry-points."metarelay.backends"]
    mybackend = "my_package.backend:MyBackend"

``connect()`` imports only the backend it needs, so a missing extra
(``labapi`` for LabArchives, say) only matters when someone uses it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import Protocol, runtime_checkable

from metarelay.core.auth import AuthFlow, LoginChallenge
from metarelay.core.config import DestinationConfig
from metarelay.core.destination import Destination
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


@runtime_checkable
class Backend(Protocol):
    """What an entry point in ``metarelay.backends`` must provide.

    The entry point may name a class (instantiated with no arguments) or an
    instance. ``auth_flow`` may be an ``AuthFlow`` or its string value
    (``"redirect"``).
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
    """Find and import the backend registered for ``scheme``.

    Anything that goes wrong while loading a third-party backend (a missing
    extra, a typo in the entry point, a constructor that needs arguments)
    becomes ``ConfigurationError``.
    """
    available: dict[str, EntryPoint] = {}
    for candidate in _entry_points():
        existing = available.setdefault(candidate.name, candidate)
        # The same entry point seen twice (a package on sys.path twice) is fine.
        if candidate.name == scheme and existing.value != candidate.value:
            raise ConfigurationError(
                f"Two installed packages register a {scheme}:// backend "
                f"({existing.value} and {candidate.value}). Uninstall one of them."
            )
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
        # Name the real error: the extra may be installed and something else broken.
        message = f"The {scheme}:// backend could not load ({error})."
        if scheme in _EXTRA_FOR_SCHEME:
            message += f" {_install_hint(scheme)}"
        raise ConfigurationError(message) from error
    except Exception as error:
        raise ConfigurationError(
            f"The {scheme}:// entry point ({entry_point.value}) could not load: {error}"
        ) from error
    try:
        backend = loaded() if isinstance(loaded, type) else loaded
    except Exception as error:
        raise ConfigurationError(
            f"The {scheme}:// backend ({entry_point.value}) could not start: {error}"
        ) from error
    if not isinstance(backend, Backend):
        raise ConfigurationError(
            f"The {scheme}:// entry point ({entry_point.value}) is not a MetaRelay backend."
        )
    _flow_of(backend, scheme)
    return backend


def _flow_of(backend: Backend, scheme: str) -> AuthFlow:
    # Compare by value, so a plugin that wrote auth_flow = "redirect" still works.
    try:
        return AuthFlow(backend.auth_flow)
    except ValueError as error:
        flows = ", ".join(flow.value for flow in AuthFlow)
        raise ConfigurationError(
            f"The {scheme}:// backend has auth_flow {backend.auth_flow!r}; expected one of {flows}."
        ) from error


def auth_flow(uri: str) -> AuthFlow:
    """Which login flow ``uri`` needs, so a UI knows what to show."""
    scheme = Destination.parse(uri).scheme
    return _flow_of(load_backend(scheme), scheme)


def connect(uri: str, config: DestinationConfig | None = None) -> Session:
    """Open a session to ``uri``.

    For a destination that needs a browser login (``AuthFlow.REDIRECT``), use
    ``begin_login`` and ``complete_login`` instead.
    """
    destination = Destination.parse(uri)
    backend = load_backend(destination.scheme)
    if _flow_of(backend, destination.scheme) is AuthFlow.REDIRECT:
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
    if _flow_of(backend, destination.scheme) is not AuthFlow.REDIRECT:
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
    if _flow_of(backend, destination.scheme) is not AuthFlow.REDIRECT:
        raise ConfigurationError(f"{destination.scheme}:// does not use a browser login.")
    return backend.complete_login(challenge, params, config)
