import tomllib
from importlib.metadata import requires
from pathlib import Path

from packaging.requirements import Requirement

import metarelay

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_matches_pyproject() -> None:
    with PYPROJECT.open("rb") as handle:
        expected = tomllib.load(handle)["project"]["version"]
    assert metarelay.__version__ == expected


def test_base_install_depends_only_on_pydantic() -> None:
    base = {
        Requirement(spec).name
        for spec in requires("metarelay") or []
        if Requirement(spec).marker is None
    }
    assert base == {"pydantic", "pydantic-settings"}
