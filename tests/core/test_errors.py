import pytest

import metarelay
from metarelay.core import errors

ALL_ERRORS = [
    errors.ConfigurationError,
    errors.AuthenticationError,
    errors.BackendUnavailableError,
    errors.NotFoundError,
    errors.ConflictError,
    errors.ValidationError,
    errors.PermissionDeniedError,
]


@pytest.mark.parametrize("error_class", ALL_ERRORS)
def test_every_error_shares_one_base(error_class: type[Exception]) -> None:
    assert issubclass(error_class, errors.MetaRelayError)
    with pytest.raises(errors.MetaRelayError):
        raise error_class("boom")


@pytest.mark.parametrize("error_class", [*ALL_ERRORS, errors.MetaRelayError])
def test_every_error_is_exported_from_the_package(error_class: type[Exception]) -> None:
    assert getattr(metarelay, error_class.__name__) is error_class


def test_validation_error_is_also_a_value_error() -> None:
    assert issubclass(errors.ValidationError, ValueError)


def test_errors_are_distinct() -> None:
    assert len(set(ALL_ERRORS)) == len(ALL_ERRORS)
    for error_class in ALL_ERRORS:
        others = [other for other in ALL_ERRORS if other is not error_class]
        assert not any(issubclass(error_class, other) for other in others)
