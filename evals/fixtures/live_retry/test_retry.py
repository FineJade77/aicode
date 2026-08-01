import pytest

from retry import call_with_retries


def make_operation(failures: int):
    state = {"calls": 0}

    def operation():
        state["calls"] += 1
        if state["calls"] <= failures:
            raise ValueError("not yet")
        return state["calls"]

    return operation


def test_single_attempt_still_calls_once():
    assert call_with_retries(make_operation(0), 1) == 1


def test_succeeds_on_the_last_allowed_attempt():
    assert call_with_retries(make_operation(2), 3) == 3


def test_raises_after_exhausting_attempts():
    with pytest.raises(ValueError):
        call_with_retries(make_operation(5), 3)
