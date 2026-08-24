from stats import mean


def test_a_normal_average_is_unchanged():
    assert mean([1, 2, 3]) == 2
