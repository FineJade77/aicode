from pagination import paginate

ITEMS = [1, 2, 3, 4, 5]


def test_first_page_starts_at_the_first_item():
    assert paginate(ITEMS, 1, 2) == [1, 2]


def test_second_page_continues_where_the_first_ended():
    assert paginate(ITEMS, 2, 2) == [3, 4]


def test_last_page_may_be_partial():
    assert paginate(ITEMS, 3, 2) == [5]
