from records import dedupe


def test_identical_ids_still_collapse():
    rows = [{"id": 1, "email": "a@example.com"}, {"id": 1, "email": "a@example.com"}]
    assert len(dedupe(rows)) == 1
