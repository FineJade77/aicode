from datetime import datetime, timezone

from deadlines import is_overdue

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_aware_deadline_in_the_past():
    assert is_overdue(datetime(2025, 1, 1, tzinfo=timezone.utc), NOW) is True


def test_naive_deadline_is_read_as_utc():
    assert is_overdue(datetime(2025, 1, 1), NOW) is True


def test_naive_future_deadline_is_not_overdue():
    assert is_overdue(datetime(2027, 1, 1), NOW) is False
