from trace import first_unset, walk


def test_every_stage_is_numbered_in_order():
    assert first_unset() == ""
    assert [value for _, value in walk()] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
