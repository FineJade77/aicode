from normalize import normalize


def test_lowercases():
    assert normalize("ABC") == "abc"


def test_trims_whitespace():
    assert normalize("  abc  ") == "abc"


def test_does_both():
    assert normalize("  MiXeD  ") == "mixed"
