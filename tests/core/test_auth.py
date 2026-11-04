import pydantic
import pytest
from pydantic_settings import SettingsConfigDict

from metarelay import (
    AuthFlow,
    ConfigurationError,
    DestinationConfig,
    LoginChallenge,
    TokenConfig,
    ValidationError,
)


def test_auth_flows() -> None:
    assert [flow.value for flow in AuthFlow] == ["none", "token", "redirect"]


def test_login_challenge_is_plain_data() -> None:
    challenge = LoginChallenge(
        destination="labarchives://api.labarchives.com",
        auth_url="https://example.test/login?x=1",
        redirect_url="http://localhost:8501/callback",
        state={"nonce": "abc"},
    )
    assert LoginChallenge.model_validate_json(challenge.model_dump_json()) == challenge


def test_token_is_never_printed() -> None:
    config = TokenConfig(token="s3cret")
    assert "s3cret" not in repr(config)
    assert "s3cret" not in str(config)
    assert "s3cret" not in config.model_dump_json()
    assert config.token.get_secret_value() == "s3cret"


def test_login_challenge_destination_is_normalized() -> None:
    challenge = LoginChallenge(destination="LabArchives://API.x/", auth_url="u", redirect_url="r")
    assert challenge.destination == "labarchives://api.x"


def test_generic_token_config_ignores_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # A shared METARELAY_TOKEN would send one service's token to another.
    monkeypatch.setenv("METARELAY_TOKEN", "github-token")
    with pytest.raises(ConfigurationError):
        TokenConfig()


def test_missing_token_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="token"):
        TokenConfig()


def test_config_errors_never_echo_the_values_passed_in() -> None:
    with pytest.raises(ConfigurationError) as caught:
        TokenConfig(token="s3cret", tokn="s3cret-typo")
    message = str(caught.value)
    assert "tokn" in message
    assert "s3cret" not in message
    assert caught.value.__cause__ is None


def test_backends_subclass_with_their_own_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExampleConfig(DestinationConfig):
        model_config = SettingsConfigDict(env_prefix="METARELAY_EXAMPLE_")
        api_key: pydantic.SecretStr

    monkeypatch.setenv("METARELAY_EXAMPLE_API_KEY", "k")
    assert ExampleConfig().api_key.get_secret_value() == "k"


def test_a_backend_token_config_reads_only_its_own_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GitHubConfig(TokenConfig):
        model_config = SettingsConfigDict(env_prefix="METARELAY_GITHUB_")

    class ElabConfig(TokenConfig):
        model_config = SettingsConfigDict(env_prefix="METARELAY_ELABFTW_")

    monkeypatch.setenv("METARELAY_GITHUB_TOKEN", "github-token")
    assert GitHubConfig().token.get_secret_value() == "github-token"
    with pytest.raises(ConfigurationError):
        ElabConfig()


def test_explicit_value_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    class GitHubConfig(TokenConfig):
        model_config = SettingsConfigDict(env_prefix="METARELAY_GITHUB_")

    monkeypatch.setenv("METARELAY_GITHUB_TOKEN", "from-env")
    assert GitHubConfig(token="explicit").token.get_secret_value() == "explicit"


def test_configs_reject_unknown_fields() -> None:
    with pytest.raises(ConfigurationError):
        TokenConfig(token="x", tokn="typo")


def test_bad_login_challenge_json_is_a_metarelay_error() -> None:
    with pytest.raises(ValidationError):
        LoginChallenge.model_validate_json('{"destination": "not a uri"}')
