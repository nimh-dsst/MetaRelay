import pytest

from metarelay import ROOT, Container, LogicalPath, ValidationError


def test_root_is_empty() -> None:
    assert ROOT.parts == ()
    assert str(ROOT) == ""
    assert ROOT.name == ""
    assert len(ROOT) == 0


def test_build_and_walk() -> None:
    path = ROOT / "Study" / "Cohort A" / "M1"
    assert path.parts == ("Study", "Cohort A", "M1")
    assert path.name == "M1"
    assert path.parent == LogicalPath(("Study", "Cohort A"))
    assert str(path) == "Study/Cohort A/M1"


def test_parse_round_trips_str() -> None:
    path = LogicalPath(["Study", "Cohort A"])
    assert LogicalPath.parse(str(path)) == path
    assert LogicalPath.parse("") == ROOT
    assert LogicalPath.parse("/a//b/") == LogicalPath(("a", "b"))


@pytest.mark.parametrize("bad", ["", "  ", "a/b", ".", "..", " padded"])
def test_bad_names_are_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        LogicalPath(("ok", bad))


def test_a_bare_string_is_rejected_not_split_into_letters() -> None:
    with pytest.raises(ValidationError, match="not a string"):
        LogicalPath("Study")  # type: ignore[arg-type]


def test_a_non_iterable_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        LogicalPath(5)  # type: ignore[arg-type]


def test_paths_are_hashable_and_comparable() -> None:
    assert {LogicalPath(("a",)), LogicalPath(["a"])} == {LogicalPath(("a",))}


def test_path_serializes_as_a_plain_list() -> None:
    container = Container(path=LogicalPath(("a", "b")), native_id="123")
    assert container.model_dump(mode="json") == {"path": ["a", "b"], "native_id": "123"}
    assert Container.model_validate(container.model_dump(mode="json")) == container
    assert container.name == "b"
