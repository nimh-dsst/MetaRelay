"""The credentials_env_file example, run as its docstring shows."""

import pytest


def test_connects_with_the_token_in_the_env_file(run_module, monkeypatch) -> None:
    pytest.importorskip("httpx")
    # Variables already in the environment would win over the file.
    monkeypatch.delenv("METARELAY_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("METARELAY_GITHUB_API_URL", raising=False)
    result = run_module("examples.credentials_env_file")
    assert result.returncode == 0, result.stderr
    assert "Connected to github://my-lab/metadata@main/records" in result.stdout
