import pydantic
import pytest
from pydantic_settings import SettingsConfigDict

from metarelay import (
    ConfigurationError,
    DestinationConfig,
    LoginChallenge,
    TokenConfig,
)


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
    with pytest.raises(ConfigurationError, match="token"):
        TokenConfig()


def test_config_errors_never_echo_the_values_passed_in() -> None:
    with pytest.raises(ConfigurationError) as caught:
        TokenConfig(token="s3cret", tokn="s3cret-typo")
    message = str(caught.value)
    assert "tokn" in message
    assert "s3cret" not in message
    assert caught.value.__cause__ is None


def test_a_backend_config_reads_only_its_own_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExampleConfig(DestinationConfig):
        model_config = SettingsConfigDict(env_prefix="METARELAY_EXAMPLE_")
        api_key: pydantic.SecretStr

    monkeypatch.setenv("METARELAY_EXAMPLE_API_KEY", "k")
    assert ExampleConfig().api_key.get_secret_value() == "k"

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


@pytest.mark.parametrize(
    ("backend", "config_name", "values"),
    [
        ("elabftw", "ElabFTWConfig", {"token": "elab-key"}),
        ("github", "GitHubConfig", {"token": "github-token"}),
        ("labarchives", "LabArchivesConfig", {"akid": "akid-1", "akpass": "akpass-1"}),
    ],
)
def test_a_backend_config_reads_a_dotenv_file_only_when_named(
    backend: str, config_name: str, values: dict[str, str], tmp_path, monkeypatch
) -> None:
    config_class = getattr(pytest.importorskip(f"metarelay.backends.{backend}"), config_name)
    prefix = config_class.model_config["env_prefix"]
    names = [prefix + field.upper() for field in values]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    lines = [f"{name}={value}\n" for name, value in zip(names, values.values(), strict=True)]
    (tmp_path / ".env").write_text("".join(lines) + "OTHER_APP_SETTING=1\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigurationError):
        config_class()
    assert config_class(_env_file=".env") == config_class(**values)
