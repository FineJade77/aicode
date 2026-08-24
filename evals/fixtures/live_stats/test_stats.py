from stats import mean, spread


def test_mean_of_several_values():
    assert mean([1, 2, 3]) == 2


def test_spread_of_several_values():
    assert spread([1, 5]) == 4
