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


def test_every_error_shares_one_base_and_none_overlap() -> None:
    assert len(set(ALL_ERRORS)) == len(ALL_ERRORS)
    for error_class in ALL_ERRORS:
        assert issubclass(error_class, errors.MetaRelayError), error_class
        others = [other for other in ALL_ERRORS if other is not error_class]
        assert not any(issubclass(error_class, other) for other in others), error_class


def test_every_error_is_exported_from_the_package() -> None:
    for error_class in [*ALL_ERRORS, errors.MetaRelayError]:
        assert getattr(metarelay, error_class.__name__) is error_class, error_class


def test_validation_error_is_also_a_value_error() -> None:
    assert issubclass(errors.ValidationError, ValueError)
