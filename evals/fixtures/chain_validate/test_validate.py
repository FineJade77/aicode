from validate import first_problem


def test_no_problems_remain():
    assert first_problem() == ""
