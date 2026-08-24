from report import price_table


def test_each_currency_gets_its_own_rate():
    table = price_table(100)
    assert table["USD"] == 100.0
    assert table["EUR"] == 110.0
    assert table["GBP"] == 130.0


def test_rates_are_not_shared_across_currencies():
    table = price_table(100)
    assert len(set(table.values())) == 3
