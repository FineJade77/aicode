from parser import parse_pair


def test_parses_a_pair():
    assert parse_pair("host = example.com") == ("host", "example.com")
