import pytest

from errors import AppError, ConfigError
from loader import load
from validator import validate


def test_config_error_is_an_app_error():
    assert issubclass(ConfigError, AppError)


def test_loader_raises_config_error():
    with pytest.raises(ConfigError):
        load({})


def test_validator_raises_config_error():
    with pytest.raises(ConfigError):
        validate({"name": "x", "retries": -1})


def test_valid_config_passes_through():
    assert validate(load({"name": "x", "retries": 1})) == {"name": "x", "retries": 1}
