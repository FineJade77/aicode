from digest import by_day
from parse import parse_timestamp
from store import Store


def test_late_evening_utc_stays_on_its_own_day():
    store = Store()
    store.add("a", parse_timestamp("2026-03-01T23:30:00+00:00"))
    store.add("b", parse_timestamp("2026-03-02T00:30:00+00:00"))

    assert by_day(store.all()) == {"2026-03-01": ["a"], "2026-03-02": ["b"]}


def test_an_offset_input_is_normalised_once():
    store = Store()
    store.add("c", parse_timestamp("2026-03-01T20:30:00-05:00"))  # 01:30 UTC on the 2nd

    assert by_day(store.all()) == {"2026-03-02": ["c"]}
