"""Config objects: how credentials reach a backend.

MetaRelay never looks for credentials on its own and never saves them. The
caller builds a config object and passes it to ``connect()``.

Config objects are ``pydantic-settings`` models. A config reads environment
variables only if it declares its own ``env_prefix``, so a token is only ever
read from a variable named for its service. It reads a ``.env`` file only when
the caller names one with ``_env_file=``::

    class GitHubConfig(TokenConfig):
        model_config = SettingsConfigDict(env_prefix="METARELAY_GITHUB_")

    GitHubConfig()                 # reads METARELAY_GITHUB_TOKEN
    GitHubConfig(_env_file=".env") # ...or from that file, if not in the environment
    TokenConfig(token="...")       # the generic class: explicit values only

There is deliberately no shared ``METARELAY_TOKEN``: a GitHub token in that
variable would be sent to whichever service the next ``connect()`` named.

Secrets are ``SecretStr``: they print as ``**********`` and are left out of
``repr`` and JSON dumps unless unwrapped with ``.get_secret_value()``. Invalid
config raises ``metarelay.ConfigurationError``, and the message never includes
the values passed in.
"""

from __future__ import annotations

from typing import Any

import pydantic
from pydantic import SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from metarelay.core.errors import ConfigurationError
from metarelay.core.model import describe_errors

__all__ = ["DestinationConfig", "TokenConfig"]


class DestinationConfig(BaseSettings):
    """Base class for every backend's config."""

    # A .env file named with _env_file= may hold other apps' variables: read
    # only those with this config's prefix.
    model_config = SettingsConfigDict(extra="forbid", frozen=True, dotenv_filtering="match_prefix")

    def __init__(self, **values: Any) -> None:
        try:
            super().__init__(**values)
        except pydantic.ValidationError as error:
            # The input may be a token: never echo it, never chain it.
            problems = describe_errors(error, "config")
            raise ConfigurationError(f"Invalid {type(self).__name__}: {problems}") from None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        if not settings_cls.model_config.get("env_prefix"):
            return (init_settings,)
        return init_settings, env_settings, dotenv_settings, file_secret_settings


class TokenConfig(DestinationConfig):
    """A single API key or access token, passed explicitly.

    Backends subclass it with an ``env_prefix`` to also read it from the
    environment.
    """

    token: SecretStr
