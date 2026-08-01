from api import created, ok


def test_ok():
    assert ok("x") == (200, "x")


def test_created():
    assert created("x") == (201, "x")
