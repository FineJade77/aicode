from percentiles import percentile

VALUES = list(range(1, 11))


def test_median():
    assert percentile(VALUES, 50) == 5


def test_p95():
    assert percentile(VALUES, 95) == 10


def test_p100_is_the_maximum():
    assert percentile(VALUES, 100) == 10


def test_single_value():
    assert percentile([7], 50) == 7
