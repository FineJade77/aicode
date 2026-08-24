from report import build_report, column_count
from row_format import format_row


def test_rows_are_comma_separated():
    assert format_row(["a", "b"]) == "a,b"


def test_report_joins_rows_with_newlines():
    assert build_report([["a", "b"], ["c", "d"]]) == "a,b\nc,d"


def test_column_count_reads_the_new_separator():
    assert column_count(format_row(["a", "b", "c"])) == 3
