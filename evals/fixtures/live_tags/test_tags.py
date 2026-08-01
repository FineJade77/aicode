from tags import add_tag


def test_adds_a_new_tag():
    assert add_tag(["a"], "b") == ["a", "b"]
