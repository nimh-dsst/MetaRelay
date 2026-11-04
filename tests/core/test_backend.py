from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from metarelay import (
    AuthFlow,
    ConfigurationError,
    Destination,
    LoginChallenge,
    TokenConfig,
    auth_flow,
    begin_login,
    complete_login,
    connect,
)
from metarelay.core import backend as backend_module


class OpenedSession:
    """Stands in for a real session; connect() only passes it through."""

    def __init__(self, destination: Destination, config: Any) -> None:
        self.destination = destination
        self.config = config


class TokenBackend:
    name = "fake"
    auth_flow = AuthFlow.TOKEN

    def open(self, destination, config):
        return OpenedSession(destination, config)

    def begin_login(self, destination, redirect_url, config):
        raise AssertionError("not used")

    def complete_login(self, challenge, params, config):
        raise AssertionError("not used")


class RedirectBackend(TokenBackend):
    name = "labarchives"
    auth_flow = AuthFlow.REDIRECT

    def begin_login(self, destination, redirect_url, config):
        return LoginChallenge(
            destination=destination.uri,
            auth_url="https://login.test/?r=" + redirect_url,
            redirect_url=redirect_url,
            state={"step": "1"},
        )

    def complete_login(self, challenge: LoginChallenge, params: Mapping[str, str], config):
        return OpenedSession(Destination.parse(challenge.destination), dict(params))


@dataclass
class FakeEntryPoint:
    name: str
    loader: Callable[[], Any]
    value: str = "fake.module:Backend"

    def load(self) -> Any:
        return self.loader()


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch):
    def _install(*entry_points: FakeEntryPoint) -> None:
        monkeypatch.setattr(backend_module, "_entry_points", lambda: list(entry_points))

    return _install


class TestDestination:
    def test_parse(self) -> None:
        destination = Destination.parse("github://my-lab/metadata@main/records")
        assert destination.scheme == "github"
        assert destination.netloc == "my-lab"
        assert destination.path == "/metadata@main/records"
        assert str(destination) == "github://my-lab/metadata@main/records"

    def test_file_uri(self) -> None:
        destination = Destination.parse("file:///Users/me/metadata")
        assert (destination.scheme, destination.netloc, destination.path) == (
            "file",
            "",
            "/Users/me/metadata",
        )

    def test_scheme_is_lowercased(self) -> None:
        assert Destination.parse("ELABFTW://host/api/v2").scheme == "elabftw"

    @pytest.mark.parametrize(
        ("uri", "normal"),
        [
            ("LabArchives://API.Example.org/", "labarchives://api.example.org"),
            ("labarchives://api.example.org", "labarchives://api.example.org"),
            ("elabftw://Elab.org/api/v2/", "elabftw://elab.org/api/v2"),
            ("file:///Users/Me/Metadata/", "file:///Users/Me/Metadata"),
            ("file:///", "file:///"),
        ],
    )
    def test_uris_are_normalized(self, uri: str, normal: str) -> None:
        assert Destination.parse(uri).uri == normal

    def test_credentials_in_the_uri_are_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="config object") as caught:
            Destination.parse("elabftw://user:hunter2@elab.org")
        assert "hunter2" not in str(caught.value)

    @pytest.mark.parametrize("bad", ["/just/a/path", "", "no scheme", "1bad://x"])
    def test_rejects_non_uris(self, bad: str) -> None:
        with pytest.raises(ConfigurationError):
            Destination.parse(bad)


def test_connect_passes_destination_and_config(installed) -> None:
    installed(FakeEntryPoint("fake", lambda: TokenBackend))
    config = TokenConfig(token="t")
    session = connect("fake://host/path", config)
    assert session.destination.netloc == "host"
    assert session.config is config


def test_entry_point_can_be_an_instance(installed) -> None:
    installed(FakeEntryPoint("fake", lambda: TokenBackend()))
    assert connect("fake://x").destination.scheme == "fake"


@pytest.mark.parametrize("scheme", ["labarchives", "elabftw", "github"])
def test_missing_builtin_backend_says_which_extra_to_install(installed, scheme: str) -> None:
    installed()
    with pytest.raises(ConfigurationError, match=rf"pip install 'metarelay\[{scheme}\]'"):
        connect(f"{scheme}://host")


def test_backend_whose_extra_is_missing_says_which_extra_to_install(installed) -> None:
    def fail() -> Any:
        raise ModuleNotFoundError("No module named 'labapi'")

    installed(FakeEntryPoint("labarchives", fail))
    with pytest.raises(ConfigurationError, match=r"metarelay\[labarchives\]"):
        auth_flow("labarchives://host")


def test_unknown_scheme_lists_what_is_installed(installed) -> None:
    installed(FakeEntryPoint("fake", lambda: TokenBackend))
    with pytest.raises(ConfigurationError, match=r"Installed backends: fake://"):
        connect("nope://x")


def test_entry_point_that_is_not_a_backend_is_rejected(installed) -> None:
    installed(FakeEntryPoint("fake", lambda: object()))
    with pytest.raises(ConfigurationError, match="not a MetaRelay backend"):
        connect("fake://x")


def test_auth_flow_reports_the_backends_flow(installed) -> None:
    installed(
        FakeEntryPoint("fake", lambda: TokenBackend),
        FakeEntryPoint("labarchives", lambda: RedirectBackend),
    )
    assert auth_flow("fake://x") is AuthFlow.TOKEN
    assert auth_flow("labarchives://x") is AuthFlow.REDIRECT


class TestRedirectLogin:
    @pytest.fixture(autouse=True)
    def _backends(self, installed) -> None:
        installed(
            FakeEntryPoint("fake", lambda: TokenBackend),
            FakeEntryPoint("labarchives", lambda: RedirectBackend),
        )

    def test_begin_then_complete(self) -> None:
        challenge = begin_login("labarchives://api.test", "http://localhost/cb")
        assert challenge.auth_url.startswith("https://login.test/")
        # A web app stores the challenge between requests.
        restored = LoginChallenge.model_validate_json(challenge.model_dump_json())
        session = complete_login(restored, {"auth_code": "123", "email": "a@b.test"})
        assert session.destination.netloc == "api.test"
        assert session.config == {"auth_code": "123", "email": "a@b.test"}

    def test_connect_refuses_a_redirect_backend(self) -> None:
        with pytest.raises(ConfigurationError, match="begin_login"):
            connect("labarchives://api.test")

    def test_begin_login_refuses_a_token_backend(self) -> None:
        with pytest.raises(ConfigurationError, match="connect"):
            begin_login("fake://x", "http://localhost/cb")

    def test_complete_login_refuses_a_token_backend(self) -> None:
        challenge = LoginChallenge(destination="fake://x", auth_url="u", redirect_url="r")
        with pytest.raises(ConfigurationError):
            complete_login(challenge, {})


def test_real_entry_point_group_is_queried() -> None:
    # No backends ship yet (#4 adds file://), so this only checks the lookup runs.
    assert isinstance(list(backend_module._entry_points()), list)


def test_auth_flow_given_as_a_plain_string_still_routes(installed) -> None:
    class StringRedirect(RedirectBackend):
        auth_flow = "redirect"  # type: ignore[assignment]

    installed(FakeEntryPoint("labarchives", lambda: StringRedirect))
    assert auth_flow("labarchives://api.x") is AuthFlow.REDIRECT
    with pytest.raises(ConfigurationError, match="browser login"):
        connect("labarchives://api.x")
    assert begin_login("labarchives://api.x", "http://localhost/cb").auth_url


def test_unknown_auth_flow_is_a_configuration_error(installed) -> None:
    class Odd(TokenBackend):
        auth_flow = "oauth"  # type: ignore[assignment]

    installed(FakeEntryPoint("fake", lambda: Odd))
    with pytest.raises(ConfigurationError, match="auth_flow"):
        connect("fake://x")


def _raise(error: Exception):
    def loader():
        raise error

    return loader


class NeedsArguments(TokenBackend):
    def __init__(self, required: str) -> None:
        self.required = required


@pytest.mark.parametrize(
    "loader",
    [
        _raise(AttributeError("module has no attribute 'Backnd'")),
        _raise(SyntaxError("bad plugin")),
        lambda: NeedsArguments,
    ],
    ids=["typo-in-entry-point", "broken-plugin", "constructor-needs-arguments"],
)
def test_any_load_failure_is_a_configuration_error(installed, loader) -> None:
    installed(FakeEntryPoint("fake", loader))
    with pytest.raises(ConfigurationError, match="fake://"):
        connect("fake://x")
