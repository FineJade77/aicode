from csv_row import split_row


def test_splits_a_plain_row():
    assert split_row("a,b,c") == ["a", "b", "c"]
