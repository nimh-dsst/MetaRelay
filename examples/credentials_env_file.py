"""Read credentials from a ``.env`` file.

MetaRelay never looks for a ``.env`` file on its own. Name it with
``_env_file=``: the underscore comes from ``pydantic-settings``, and the
argument is supported. Variables already set in the environment win over the
file.

This writes a throwaway ``.env`` into a temporary folder and connects to a
fake GitHub API in this process, so it needs no account. Needs
``pip install 'metarelay[github]'``. From the repository root::

    python -m examples.credentials_env_file
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from metarelay import MetaRelayError, Session, connect
from metarelay.backends.github import GitHubConfig
from metarelay.testing.github_fake import FakeGitHub, serve_fake

URI = "github://my-lab/metadata@main/records"
TOKEN = "fake-token"


def connect_with_env_file(uri: str, env_file: Path) -> Session:
    """Connect to ``uri`` with the token and API URL in ``env_file``."""
    # [docs:env-file]
    session = connect(uri, GitHubConfig(_env_file=env_file))
    # [/docs:env-file]
    return session


def main() -> int:
    fake = FakeGitHub(token=TOKEN)
    fake.add_repo("my-lab/metadata")
    with serve_fake(fake) as api_url, tempfile.TemporaryDirectory() as folder:
        env_file = Path(folder) / ".env"
        env_file.write_text(f"METARELAY_GITHUB_TOKEN={TOKEN}\nMETARELAY_GITHUB_API_URL={api_url}\n")
        try:
            session = connect_with_env_file(URI, env_file)
        except MetaRelayError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        session.close()
    print(f"Connected to {URI} with the token in {env_file.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
