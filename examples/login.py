"""Open a session to any destination from a terminal. Both examples use it.

Most backends open with ``connect()``. LabArchives needs a browser login,
which a library can't show, so it takes two calls with the browser in
between: ``begin_login`` gives a link, the person logs in, and LabArchives
sends their browser back to a callback address. ``complete_login`` reads the
query parameters on that address.

A web app would serve the callback address itself. A terminal script can't,
so ``ask_in_terminal`` has the person paste the address instead. The fake
LabArchives server logs in without a password, so ``follow_fake_login`` reads
the address straight from its redirect.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import parse_qsl, urlsplit

from metarelay import (
    AuthenticationError,
    AuthFlow,
    BackendUnavailableError,
    DestinationConfig,
    Session,
    auth_flow,
    begin_login,
    complete_login,
    connect,
)

CALLBACK_URL = "http://localhost:8089/callback"
"""Where LabArchives sends the browser after login. Nothing has to listen there."""

Browser = Callable[[str], str]
"""Takes the login link and returns the address the browser was sent back to."""


def ask_in_terminal(auth_url: str) -> str:
    """Have the person log in, then paste the address their browser ended up on."""
    print("Open this link and log in:\n")
    print(f"  {auth_url}\n")
    print("Your browser then goes to an address that may not load. That's expected.")
    return input("Paste that address here: ").strip()


def follow_fake_login(auth_url: str) -> str:
    """Log in to the fake LabArchives server, whose login page redirects at once."""

    class StopAtRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        urllib.request.build_opener(StopAtRedirect).open(auth_url, timeout=10)
    except urllib.error.HTTPError as error:
        if error.code in {301, 302, 303, 307} and error.headers.get("Location"):
            return error.headers["Location"]
        raise AuthenticationError(
            f"The login page answered {error.code}. Check METARELAY_LABARCHIVES_AKID "
            "and METARELAY_LABARCHIVES_AKPASS."
        ) from None
    except urllib.error.URLError:
        # Not the error itself: its message includes the signed login link.
        raise BackendUnavailableError(
            "Could not reach the login page. Is the fake running? Start it with "
            "docker compose up -d."
        ) from None
    raise AuthenticationError("The login page did not redirect back.")


def open_session(
    uri: str,
    *,
    config: DestinationConfig | None = None,
    browser: Browser = ask_in_terminal,
    callback_url: str = CALLBACK_URL,
) -> Session:
    """Open a session to ``uri``, logging in through the browser if it needs one.

    Without ``config``, each backend reads its own settings from the
    environment (``METARELAY_LABARCHIVES_*`` for LabArchives).
    """
    if auth_flow(uri) is not AuthFlow.REDIRECT:
        return connect(uri, config)
    # [docs:browser-login]
    challenge = begin_login(uri, callback_url, config)
    # The person logs in at challenge.auth_url. LabArchives then sends their
    # browser to callback_url?email=...&auth_code=..., and those query
    # parameters finish the login.
    redirected = browser(challenge.auth_url)
    params = dict(parse_qsl(urlsplit(redirected).query))
    session = complete_login(challenge, params, config)
    # [/docs:browser-login]
    return session
