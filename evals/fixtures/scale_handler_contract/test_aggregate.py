from aggregate import summarise


def records_for(*names):
    return [{"kind": name, "id": index, "amount": 5} for index, name in enumerate(names)]


def test_every_record_is_counted_exactly_once():
    records = records_for("orders", "invoices", "tariffs")

    assert summarise(records)["processed"] == 3


def test_repeated_records_are_all_counted():
    records = records_for("tariffs", "tariffs", "orders")

    assert summarise(records)["processed"] == 3


def test_unknown_kinds_are_ignored():
    assert summarise(records_for("orders", "nonsense"))["processed"] == 1
