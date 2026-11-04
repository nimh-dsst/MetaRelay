"""Config objects: how credentials reach a backend.

MetaRelay never looks for credentials on its own and never saves them. The
caller builds a config object and passes it to ``connect()``.

Config objects are ``pydantic-settings`` models. A config reads environment
variables only if it declares its own ``env_prefix``, so a token is only ever
read from a variable named for its service::

    class GitHubConfig(TokenConfig):
        model_config = SettingsConfigDict(env_prefix="METARELAY_GITHUB_")

    GitHubConfig()                 # reads METARELAY_GITHUB_TOKEN
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

__all__ = ["DestinationConfig", "TokenConfig"]


class DestinationConfig(BaseSettings):
    """Base class for every backend's config."""

    model_config = SettingsConfigDict(extra="forbid", frozen=True)

    def __init__(self, **values: Any) -> None:
        try:
            super().__init__(**values)
        except pydantic.ValidationError as error:
            # include_input=False: the input may be a token.
            problems = "; ".join(
                f"{'.'.join(str(part) for part in detail['loc']) or 'config'}: {detail['msg']}"
                for detail in error.errors(include_input=False, include_url=False)
            )
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
