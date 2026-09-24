import pytest

from rfnry_senses.errors import (
    ConfigurationError,
    InputError,
    ModelError,
    NotFoundError,
    SensesException,
    TransportError,
)


@pytest.mark.parametrize(
    "error", [ConfigurationError, InputError, ModelError, NotFoundError, TransportError]
)
def test_every_error_is_a_senses_error(error: type[Exception]) -> None:
    assert issubclass(error, SensesException) and error.__name__.endswith("Error")


def test_the_root_takes_the_exception_suffix() -> None:
    assert SensesException.__name__ == "SensesException" and SensesException.__bases__ == (
        Exception,
    )
