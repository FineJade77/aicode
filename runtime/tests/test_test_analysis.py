from app.tools.test_analysis import analyze_test_output


def test_analyze_pytest_failure_output() -> None:
    output = """
=================================== FAILURES ===================================
FAILED tests/test_app.py::test_login - AssertionError: expected ok
=========================== 1 failed, 2 passed in 0.12s ===========================
"""

    analysis = analyze_test_output("python3 -m pytest", output, 1)

    assert analysis["framework"] == "pytest"
    assert analysis["summary"] == "1 failed, 2 passed in 0.12s"
    assert analysis["failure_count"] == 1
    assert analysis["failures"][0]["path"] == "tests/test_app.py"


def test_analyze_go_test_failure_output() -> None:
    output = """--- FAIL: TestLogin (0.00s)
    auth_test.go:12: got false, want true
FAIL
FAIL    example/auth 0.123s
"""

    analysis = analyze_test_output("go test ./...", output, 1)

    assert analysis["framework"] == "go"
    assert analysis["summary"] == "go test failed in example/auth"
    assert analysis["failures"][0]["name"] == "TestLogin"
    assert analysis["failures"][0]["path"] == "auth_test.go"
    assert analysis["failures"][0]["line"] == 12
