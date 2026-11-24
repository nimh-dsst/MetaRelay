"""The Session protocol. The conformance suite checks real sessions satisfy it."""

from metarelay import Session


def test_an_incomplete_class_does_not_satisfy_the_protocol() -> None:
    class Partial:
        def close(self) -> None: ...

    assert not isinstance(Partial(), Session)
