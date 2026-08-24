from money import split_bill


def test_exact_division():
    assert split_bill(900, 3) == [300, 300, 300]


def test_remainder_is_not_lost():
    parts = split_bill(1000, 3)
    assert sum(parts) == 1000


def test_parts_differ_by_at_most_one_cent():
    parts = split_bill(1000, 3)
    assert max(parts) - min(parts) <= 1


def test_more_people_than_cents():
    parts = split_bill(2, 5)
    assert sum(parts) == 2
    assert max(parts) - min(parts) <= 1
