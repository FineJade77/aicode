from audit import first_bad_step


def test_every_balance_is_settled():
    assert first_bad_step() == ""
