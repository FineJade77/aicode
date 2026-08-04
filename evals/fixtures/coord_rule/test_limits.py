import pytest

from bios import submit_bio
from comments import submit_comment
from titles import submit_title


def test_each_field_has_its_own_ceiling():
    assert submit_title("t" * 80)
    assert submit_comment("c" * 1000)
    assert submit_bio("b" * 300)


def test_each_field_rejects_beyond_its_own_ceiling():
    for submit, size in ((submit_title, 81), (submit_comment, 1001), (submit_bio, 301)):
        with pytest.raises(ValueError):
            submit("x" * size)
