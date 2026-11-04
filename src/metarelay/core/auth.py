"""Login flows.

Each backend uses one of three flows:

- ``NONE``: nothing to do (the local backend).
- ``TOKEN``: the caller passes an API key or token in a config object
  (eLabFTW, GitHub).
- ``REDIRECT``: the user logs in on the destination's own website
  (LabArchives). This is a begin/complete pair, because the library cannot
  show a login page:

  1. ``begin_login(uri, redirect_url)`` returns a ``LoginChallenge``.
  2. The application sends the user to ``challenge.auth_url``. The
     destination redirects back to ``redirect_url`` with some query
     parameters.
  3. ``complete_login(challenge, params)`` checks them and returns a
     ``Session``.

``LoginChallenge`` is plain data, so a web app can store it between the two
HTTP requests. ``begin_login`` and ``complete_login`` live in
``metarelay.core.connect`` with ``connect()``.
"""

from __future__ import annotations

from enum import StrEnum

import pydantic
from pydantic import ConfigDict, Field

__all__ = ["AuthFlow", "LoginChallenge"]


class AuthFlow(StrEnum):
    """How a backend logs in."""

    NONE = "none"
    TOKEN = "token"
    REDIRECT = "redirect"


class LoginChallenge(pydantic.BaseModel):
    """The first half of a redirect login, for the application to show."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    destination: str
    """The destination URI being logged in to."""
    auth_url: str
    """Send the user here."""
    redirect_url: str
    """Where the destination sends the user back to. Chosen by the application."""
    state: dict[str, str] = Field(default_factory=dict)
    """Anything the backend needs to finish the login. Not a secret, but don't
    show it to the user."""
