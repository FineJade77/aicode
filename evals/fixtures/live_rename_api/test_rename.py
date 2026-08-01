import store
from report import build_report


def test_the_store_exposes_list_records():
    assert [record["label"] for record in store.list_records()] == ["alpha", "beta"]


def test_the_old_name_is_gone():
    assert not hasattr(store, "fetch_all")


def test_the_report_still_works():
    assert build_report() == "alpha, beta"
