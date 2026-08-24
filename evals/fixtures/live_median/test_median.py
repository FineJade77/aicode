from median import median


def test_odd_count():
    assert median([3, 1, 2]) == 2


def test_even_count():
    assert median([1, 2, 3, 4]) == 2.5


def test_even_count_with_negatives():
    assert median([-5, -1, 1, 5]) == 0
