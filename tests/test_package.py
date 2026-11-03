import tomllib
from importlib.metadata import metadata, requires
from pathlib import Path

from packaging.requirements import Requirement

import metarelay

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
EXTRAS = {"labarchives", "elabftw", "github", "nwb", "testing"}


def _requirements() -> list[Requirement]:
    return [Requirement(spec) for spec in requires("metarelay") or []]


def _names_for_extra(extra: str) -> set[str]:
    return {
        req.name
        for req in _requirements()
        if req.marker is not None and req.marker.evaluate({"extra": extra})
    }


def test_version_matches_pyproject() -> None:
    with PYPROJECT.open("rb") as handle:
        expected = tomllib.load(handle)["project"]["version"]
    assert metarelay.__version__ == expected


def test_base_install_depends_only_on_pydantic() -> None:
    base = {req.name for req in _requirements() if req.marker is None}
    assert base == {"pydantic", "pydantic-settings"}


def test_declares_every_extra() -> None:
    declared = set(metadata("metarelay").get_all("Provides-Extra") or [])
    assert declared == EXTRAS | {"all"}


def test_all_extra_includes_every_other_extra() -> None:
    expected = set().union(*(_names_for_extra(extra) for extra in EXTRAS))
    assert _names_for_extra("all") >= expected
