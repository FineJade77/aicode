from export import to_csv


def test_plain_fields_are_not_quoted():
    assert to_csv([{"id": "1", "name": "Ada", "notes": "ok"}]) == "id,name,notes\n1,Ada,ok\n"


def test_a_comma_forces_quoting():
    assert to_csv([{"id": "1", "name": "Lovelace, Ada", "notes": ""}]) == (
        'id,name,notes\n1,"Lovelace, Ada",\n'
    )


def test_an_embedded_quote_is_doubled():
    assert to_csv([{"id": "1", "name": 'A "B"', "notes": ""}]) == (
        'id,name,notes\n1,"A ""B""",\n'
    )


def test_a_missing_field_is_empty_not_none():
    assert to_csv([{"id": "1"}]) == "id,name,notes\n1,,\n"
