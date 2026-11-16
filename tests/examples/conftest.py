"""Shared setup for the example tests.

The examples register ``lab.implant`` when they are imported, but every test
gets a fresh registry (``tests/conftest.py``), so register it again here.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from examples.lab_schemas import Implant
from metarelay.core.registry import SchemaRegistry
from metarelay.testing.labarchives_fake import FakeLabArchives, serve_fake

REPO = Path(__file__).resolve().parents[2]
AKID, AKPASS = "example-akid", "example-akpass"


@pytest.fixture(autouse=True)
def lab_schemas(isolated_registry: SchemaRegistry) -> None:
    isolated_registry.register("lab.implant", 1, Implant)


@pytest.fixture
def fake_labarchives(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeLabArchives, str]]:
    """The dev stack's fake LabArchives, served in this process.

    Yields the fake and its ``labarchives://`` URI, with the
    ``METARELAY_LABARCHIVES_*`` variables set to reach it.
    """
    fake = FakeLabArchives.from_seed(
        REPO / "docker" / "fake-labarchives" / "seed.json", akid=AKID, akpass=AKPASS
    )
    with serve_fake(fake) as url:
        monkeypatch.setenv("METARELAY_LABARCHIVES_AKID", AKID)
        monkeypatch.setenv("METARELAY_LABARCHIVES_AKPASS", AKPASS)
        monkeypatch.setenv("METARELAY_LABARCHIVES_API_URL", url)
        yield fake, "labarchives://" + url.removeprefix("http://")


@pytest.fixture
def elabftw(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """The dev stack's eLabFTW, if ``METARELAY_ELABFTW_TEST_API_URL`` is set.

    Yields its ``elabftw://`` URI and a fresh top-level name to work under,
    with the ``METARELAY_ELABFTW_*`` variables set to reach it.
    """
    pytest.importorskip("elabapi_python")
    api_url = os.environ.get("METARELAY_ELABFTW_TEST_API_URL")
    if not api_url:
        pytest.skip("set METARELAY_ELABFTW_TEST_API_URL to run against eLabFTW")
    monkeypatch.setenv("METARELAY_ELABFTW_TOKEN", "metarelay-dev-key")
    monkeypatch.setenv("METARELAY_ELABFTW_API_URL", api_url)
    host = api_url.split("://", 1)[1].split("/", 1)[0]
    return f"elabftw://{host}/experiments", f"Example run {uuid.uuid4().hex[:10]}"


@pytest.fixture
def run_module() -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run an example the way its README does: ``python -m examples.<name>``.

    The child process gets this process's environment, including any
    variables another fixture set.
    """

    def run(module: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=REPO,
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=60,
        )

    return run
