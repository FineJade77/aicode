from audit import summarise
from strict import StrictPipeline


def test_results_are_transformed():
    assert summarise(StrictPipeline(), [" a ", "b"])["results"] == ["A", "B"]


def test_blank_items_are_skipped():
    assert summarise(StrictPipeline(), ["a", "  ", None])["results"] == ["A"]


def test_the_audit_counts_what_was_applied():
    assert summarise(StrictPipeline(), [" a ", "b", "  "])["processed"] == 2
