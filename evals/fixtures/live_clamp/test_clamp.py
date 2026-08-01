from clamp import clamp


def test_value_inside_the_range_is_unchanged():
    assert clamp(5, 0, 10) == 5
