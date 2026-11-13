"""The conformance suite against real GitHub. Skipped unless configured.

Set both variables to run it::

    METARELAY_GITHUB_TOKEN=github_pat_...                       # Contents: read and write
    METARELAY_GITHUB_LIVE_DESTINATION=github://my-lab/sandbox@metarelay-ci/live

Use a sandbox repository or branch: every run commits under the prefix, in a
new folder per test, and nothing is deleted afterwards. A run makes a few
hundred commits and API calls.
"""

import os
import uuid

import pytest

from metarelay import LogicalPath, connect
from metarelay.testing import BackendConformance

pytest.importorskip("httpx")

from metarelay.backends.github import GitHubConfig

DESTINATION = os.environ.get("METARELAY_GITHUB_LIVE_DESTINATION")
RUN = uuid.uuid4().hex[:8]

pytestmark = pytest.mark.skipif(
    not (DESTINATION and os.environ.get("METARELAY_GITHUB_TOKEN")),
    reason="Set METARELAY_GITHUB_TOKEN and METARELAY_GITHUB_LIVE_DESTINATION to run.",
)


class TestGitHubLive(BackendConformance):
    @pytest.fixture
    def session(self):
        # A fresh base per test, so the exact listings the suite checks
        # aren't disturbed by earlier tests or runs.
        self.base = LogicalPath((f"run-{RUN}", uuid.uuid4().hex[:12]))
        session = connect(DESTINATION, GitHubConfig())
        session.ensure_container(self.base)
        yield session
        session.close()

    def edit_preview(self, session, path, record_id, text) -> None:
        receipt = session.read_record(path, record_id).receipt
        md_path = receipt.backend_ids["path"].removesuffix(".json") + ".md"
        entries = session._listing(path)
        session._put(
            md_path,
            text.encode(),
            sha=entries[f"{record_id}.md"].sha,
            message="Edit the preview by hand (MetaRelay live test)",
            action="edit the preview",
        )
