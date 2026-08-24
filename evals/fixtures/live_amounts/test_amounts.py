import pytest

from amounts import parse_amount


def test_plain_number():
    assert parse_amount(" 42 ") == 42


def test_thousands_separator():
    assert parse_amount("1,234") == 1234


def test_several_separators():
    assert parse_amount("1,000,000") == 1000000


def test_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_amount("twelve")
