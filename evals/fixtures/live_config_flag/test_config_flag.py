import pytest

from config import Config
from service import configure


def test_unknown_keys_are_ignored_by_default():
    assert configure(Config(), {"nope": 1})["port"] == 8080


def test_strict_defaults_to_false():
    assert Config().strict is False


def test_strict_rejects_unknown_keys():
    with pytest.raises(ValueError):
        configure(Config(strict=True), {"nope": 1})


def test_strict_still_applies_known_keys():
    assert configure(Config(strict=True), {"port": 9000})["port"] == 9000
