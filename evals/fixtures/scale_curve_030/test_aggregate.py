from aggregate import summarise


def records_for(*names):
    return [{"kind": name, "id": index, "weight": 4} for index, name in enumerate(names)]


def test_every_record_is_counted_exactly_once():
    assert summarise(records_for("order_sync", "carrier_sync"))["processed"] == 2


def test_repeated_records_are_all_counted():
    assert summarise(records_for("carrier_sync", "carrier_sync", "invoice_sync"))["processed"] == 3


def test_unknown_kinds_are_ignored():
    assert summarise(records_for("order_sync", "nonsense"))["processed"] == 1
