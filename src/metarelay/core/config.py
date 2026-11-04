"""Config objects: how credentials reach a backend.

MetaRelay never looks for credentials on its own and never saves them. The
caller builds a config object and passes it to ``connect()``. Config objects
are ``pydantic-settings`` models, so the caller can fill them explicitly or
from environment variables::

    TokenConfig(token="...")   # explicit
    TokenConfig()              # reads METARELAY_TOKEN

Secrets are ``SecretStr``: they print as ``**********`` and are left out of
``repr`` and JSON dumps unless unwrapped with ``.get_secret_value()``.

Each backend subclasses ``DestinationConfig`` with its own fields and env
prefix, for example ``LabArchivesConfig`` with ``METARELAY_LABARCHIVES_``.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["DestinationConfig", "TokenConfig"]


class DestinationConfig(BaseSettings):
    """Base class for every backend's config."""

    model_config = SettingsConfigDict(env_prefix="METARELAY_", extra="forbid", frozen=True)


class TokenConfig(DestinationConfig):
    """A single API key or access token."""

    token: SecretStr
