from settings import resolve

DEFAULTS = {
    "server": {"host": "localhost", "port": 8080, "tls": {"enabled": False, "ca": "system"}},
    "tags": ["a", "b"],
}


def test_nested_keys_survive_a_partial_override():
    resolved = resolve(DEFAULTS, {"server": {"port": 9000}}, {})
    assert resolved["server"]["host"] == "localhost"
    assert resolved["server"]["port"] == 9000
    assert resolved["server"]["tls"] == {"enabled": False, "ca": "system"}


def test_deeply_nested_overrides_merge():
    resolved = resolve(DEFAULTS, {}, {"server": {"tls": {"enabled": True}}})
    assert resolved["server"]["tls"] == {"enabled": True, "ca": "system"}


def test_lists_are_replaced_not_concatenated():
    assert resolve(DEFAULTS, {"tags": ["c"]}, {})["tags"] == ["c"]


def test_inputs_are_not_mutated():
    snapshot = {"server": {"tls": {"enabled": False, "ca": "system"}}}
    resolve(snapshot, {"server": {"tls": {"enabled": True}}}, {})
    assert snapshot["server"]["tls"]["enabled"] is False
