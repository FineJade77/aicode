from roster import sort_names


def test_ignores_case_when_ordering():
    assert sort_names(["beta", "Alpha", "gamma", "Delta"]) == ["Alpha", "beta", "Delta", "gamma"]


def test_keeps_the_original_spelling():
    assert sort_names(["McDonald", "macgregor"]) == ["macgregor", "McDonald"]
