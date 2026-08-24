import pytest

from graph import CycleError
from plan import build_order


def position(order, name):
    return order.index(name)


def test_simple_chain():
    order = build_order({"c": ["b"], "b": ["a"], "a": []})
    assert position(order, "a") < position(order, "b") < position(order, "c")


def test_diamond():
    deps = {"d": ["b", "c"], "b": ["a"], "c": ["a"], "a": []}
    order = build_order(deps)
    for node, requirements in deps.items():
        for requirement in requirements:
            assert position(order, requirement) < position(order, node)


def test_every_node_appears_once():
    order = build_order({"d": ["b", "c"], "b": ["a"], "c": ["a"], "a": []})
    assert sorted(order) == ["a", "b", "c", "d"]
    assert len(order) == len(set(order))


def test_a_cycle_is_reported():
    with pytest.raises(CycleError):
        build_order({"a": ["b"], "b": ["a"]})
