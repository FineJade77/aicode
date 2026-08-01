from collect import collect_all


def rows(*specs):
    return [{"id": index, "hidden": hidden} for index, hidden in enumerate(specs)]


def test_visible_rows_are_all_collected():
    data = rows(False, False, False, False)
    assert [row["id"] for row in collect_all(data)] == [0, 1, 2, 3]


def test_a_fully_hidden_page_does_not_end_the_walk():
    # Rows 2 and 3 are hidden, so that page is empty — but rows 4 and 5 follow.
    data = rows(False, False, True, True, False, False)
    assert [row["id"] for row in collect_all(data)] == [0, 1, 4, 5]
