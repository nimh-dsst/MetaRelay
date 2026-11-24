import pytest

from metarelay import ROOT, Container, LogicalPath, ValidationError


def test_root_is_empty() -> None:
    assert ROOT.parts == ()
    assert str(ROOT) == ""
    assert ROOT.name == ""
    assert len(ROOT.parts) == 0
    # ROOT must be truthy, or `path or default` would replace it.
    assert (ROOT or LogicalPath(("default",))) is ROOT


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


@pytest.mark.parametrize("bad", ["", " padded", "a/b", "..", "a\x00b"])
def test_bad_names_are_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        LogicalPath(("ok", bad))


@pytest.mark.parametrize(
    ("root", "match"),
    [("Study", "not a string"), (5, "not 5")],
    ids=["bare-string-not-split-into-letters", "non-iterable"],
)
def test_constructor_rejects_non_tuples(root: object, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        LogicalPath(root)  # type: ignore[arg-type]


def test_path_serializes_as_a_plain_list() -> None:
    container = Container(path=LogicalPath(("a", "b")), native_id="123")
    assert container.model_dump(mode="json") == {
        "path": ["a", "b"],
        "native_id": "123",
        "holds_records": False,
    }
    assert Container.model_validate(container.model_dump(mode="json")) == container
    assert container.name == "b"
    # Receipts saved before holds_records existed still load.
    assert Container.model_validate({"path": ["a", "b"], "native_id": "123"}) == container


@pytest.mark.parametrize("name", ["back\\slash", "C:"])
def test_names_only_unsafe_on_some_backends_are_allowed(name: str) -> None:
    assert LogicalPath((name,)).name == name
