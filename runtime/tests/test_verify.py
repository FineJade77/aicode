"""Failure extraction and the verification bound's own bookkeeping."""

from app.agent.verify import MAX_SUMMARY_CHARS, VerifyOutcome, VerifyTracker, summarize_failure


def test_pytest_failures_are_named():
    output = """
============================= test session starts ==============================
collected 42 items

tests/test_math.py ..F...                                                [ 14%]

=================================== FAILURES ===================================
____________________________________ test_add __________________________________
    def test_add():
>       assert add(1, 2) == 4
E       assert 3 == 4
=========================== short test summary info ============================
FAILED tests/test_math.py::test_add - assert 3 == 4
========================= 1 failed, 41 passed in 0.42s =========================
"""
    summary = summarize_failure(output)
    assert "FAILED tests/test_math.py::test_add" in summary
    # The whole session header must not survive: the point is that the wind-down
    # note and the event carry the cause, not the transcript.
    assert "test session starts" not in summary


def test_go_test_failures_are_named():
    output = "--- FAIL: TestAdd (0.00s)\n    add_test.go:12: got 3 want 4\nFAIL\nexit status 1\n"
    assert "--- FAIL: TestAdd" in summarize_failure(output)


def test_compiler_positions_are_kept():
    output = "src/main.go:12:5: undefined: helper\nsrc/main.go:30:1: syntax error\n"
    summary = summarize_failure(output)
    assert "src/main.go:12:5: undefined: helper" in summary


def test_unrecognised_output_falls_back_to_the_last_line():
    """A verdict is usually the last thing printed.

    Returning nothing here would be worse than a rough guess: the user would see
    an exhausted run with no reason attached at all.
    """
    output = "doing things\nmore things\nsomething went sideways\n"
    assert summarize_failure(output) == "something went sideways"


def test_empty_output_summarises_to_nothing():
    assert summarize_failure("") == ""
    assert summarize_failure("   \n  \n") == ""


def test_summary_is_bounded():
    output = "\n".join(f"FAILED tests/test_{i}.py::test_case - {'x' * 200}" for i in range(50))
    assert len(summarize_failure(output)) <= MAX_SUMMARY_CHARS


def test_duplicate_lines_are_reported_once():
    output = "FAILED tests/a.py::test_one\nFAILED tests/a.py::test_one\nFAILED tests/b.py::test_two\n"
    summary = summarize_failure(output)
    assert summary.count("tests/a.py::test_one") == 1


def failure(exit_code: int = 1) -> VerifyOutcome:
    return VerifyOutcome(command="pytest", passed=False, exit_code=exit_code, summary="FAILED a::b")


def success() -> VerifyOutcome:
    return VerifyOutcome(command="pytest", passed=True, exit_code=0)


def test_a_pass_releases_the_loop():
    tracker = VerifyTracker(limit=3)
    tracker.record(success())
    assert not tracker.should_request_verification(applied_edits=1)
    assert not tracker.exhausted(applied_edits=1)


def test_a_later_edit_invalidates_an_earlier_pass():
    tracker = VerifyTracker(limit=3)
    tracker.record(success())
    tracker.note_edit_applied()
    assert tracker.should_request_verification(applied_edits=2)


def test_the_bound_holds_without_any_recorded_attempt():
    """A model that simply never runs anything must still be bounded.

    This is the case the old one-shot flag got wrong: with nothing counting
    attempts, "I am done" twice was enough to exit.
    """
    tracker = VerifyTracker(limit=2)
    for _ in range(2):
        assert tracker.should_request_verification(applied_edits=1)
        tracker.begin_round()
    assert not tracker.should_request_verification(applied_edits=1)
    assert tracker.exhausted(applied_edits=1)


def test_a_pass_after_failures_clears_the_digest():
    tracker = VerifyTracker(limit=3)
    tracker.record(failure())
    assert "FAILED a::b" in tracker.failure_digest()
    tracker.record(success())
    assert tracker.failure_digest() == ""


def test_no_edits_means_nothing_to_verify():
    tracker = VerifyTracker(limit=3)
    assert not tracker.should_request_verification(applied_edits=0)
    assert not tracker.exhausted(applied_edits=0)


def test_zero_limit_disables_the_check_entirely():
    tracker = VerifyTracker(limit=0)
    assert not tracker.enabled
    assert not tracker.should_request_verification(applied_edits=5)
    # Critically also not "exhausted": a disabled check must let the turn end
    # normally, not divert every edit into the wind-down path.
    assert not tracker.exhausted(applied_edits=5)
