from alerts import is_slow
from budget import total_minutes
from report import format_duration
from timing import elapsed


def test_elapsed_is_measured_in_milliseconds():
    assert elapsed(10.0, 12.5) == 2500


def test_a_duration_is_reported_in_milliseconds():
    assert format_duration(10.0, 12.5) == "2500ms"


def test_the_slow_threshold_is_unchanged_in_real_time():
    # Still two and a half seconds, expressed in the new unit.
    assert is_slow(10.0, 12.6) is True
    assert is_slow(10.0, 12.4) is False


def test_totals_are_still_minutes():
    assert total_minutes([(0.0, 60.0), (0.0, 60.0)]) == 2
