import pydantic
import pytest
from pydantic_settings import SettingsConfigDict

from metarelay import AuthFlow, DestinationConfig, LoginChallenge, TokenConfig


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


def test_token_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METARELAY_TOKEN", "from-env")
    assert TokenConfig().token.get_secret_value() == "from-env"


def test_explicit_value_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METARELAY_TOKEN", "from-env")
    assert TokenConfig(token="explicit").token.get_secret_value() == "explicit"


def test_missing_token_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("METARELAY_TOKEN", raising=False)
    with pytest.raises(pydantic.ValidationError):
        TokenConfig()


def test_backends_subclass_with_their_own_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExampleConfig(DestinationConfig):
        model_config = SettingsConfigDict(env_prefix="METARELAY_EXAMPLE_")
        api_key: pydantic.SecretStr

    monkeypatch.setenv("METARELAY_EXAMPLE_API_KEY", "k")
    assert ExampleConfig().api_key.get_secret_value() == "k"


def test_configs_reject_unknown_fields() -> None:
    with pytest.raises(pydantic.ValidationError):
        TokenConfig(token="x", tokn="typo")
