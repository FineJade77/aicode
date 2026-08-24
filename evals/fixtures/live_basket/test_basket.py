from basket import add_item


def test_adds_to_a_supplied_basket():
    basket = ["milk"]
    assert add_item("bread", basket) == ["milk", "bread"]


def test_each_default_call_starts_empty():
    first = add_item("milk")
    second = add_item("bread")
    assert first == ["milk"]
    assert second == ["bread"]
