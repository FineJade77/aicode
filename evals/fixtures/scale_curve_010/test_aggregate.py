from aggregate import summarise


def records_for(*names):
    return [{"kind": name, "id": index} for index, name in enumerate(names)]


def test_every_handler_counts_its_own_records():
    assert summarise(records_for("order_sync"))["processed"] == 1


def test_no_record_is_counted_twice():
    assert summarise(records_for("customer_sync"))["processed"] == 1


def test_a_mixed_batch_totals_correctly():
    assert summarise(records_for("customer_sync", "order_sync", "customer_sync"))["processed"] == 3
