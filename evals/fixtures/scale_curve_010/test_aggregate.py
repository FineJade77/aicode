from aggregate import summarise
from registry import HANDLERS


def records_for(*names):
    return [{"kind": name, "id": index} for index, name in enumerate(names)]


def total_for(kinds):
    return summarise(records_for(*kinds))["processed"]


def test_alternating_kinds_total_correctly():
    kinds = HANDLERS[::2]
    # Both sides are bound before the comparison so the failure reports two
    # integers. Calling into the assertion makes pytest print the slice it was
    # given, which at 300 modules feeds the size of the repository into the
    # transcript and turns the curve into a measurement of its own error text.
    processed = total_for(kinds)
    assert processed == len(kinds)


def test_the_complementary_batch_totals_correctly():
    kinds = HANDLERS[1::2]
    processed = total_for(kinds)
    assert processed == len(kinds)
