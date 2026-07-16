from pathlib import Path

from app.tools.review import DiffFile, DiffLine, format_review_report, load_untracked_files, review_diff_text, review_files, review_report_data, review_rules_data


def test_review_detects_secrets_without_echoing_value() -> None:
    secret_name = "OPENAI_" + "API_KEY"
    secret_value = "sk-" + "secretsecretsecret"
    diff = f"""diff --git a/.env b/.env
--- a/.env
+++ b/.env
@@ -1 +1,2 @@
 APP_ENV=dev
+{secret_name}={secret_value}
"""

    report = review_diff_text(diff, protected_paths=[".env"])
    text = format_review_report(report)
    data = review_report_data(report)

    assert data["summary"]["by_severity"]["high"] == 2
    assert "受保护或敏感路径发生变更" in text
    assert "新增行包含疑似密钥" in text
    assert secret_value not in text
    assert secret_value not in str(data)


def test_review_detects_risky_code_and_debug_output() -> None:
    risky_call = "eval"
    diff = f"""diff --git a/app/main.ts b/app/main.ts
--- a/app/main.ts
+++ b/app/main.ts
@@ -1,2 +1,4 @@
 def run(value):
+    console.log(value)
+    return {risky_call}(value)
     return value
"""

    report = review_diff_text(diff)
    rules = {finding.rule for finding in report.findings}

    assert "debug_output" in rules
    assert "risky_eval" in rules


def test_review_formats_clean_diff() -> None:
    diff = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # aicode
+Phase 1 notes
"""

    report = review_diff_text(diff)
    text = format_review_report(report)

    assert report.added_lines == 1
    assert report.removed_lines == 0
    assert not report.findings
    assert "未发现确定性风险" in text


def test_review_scans_untracked_files(tmp_path: Path) -> None:
    new_file = tmp_path / "app.py"
    risky_call = "eval"
    new_file.write_text(f"def run(value):\n    return {risky_call}(value)\n", encoding="utf-8")

    files = load_untracked_files(tmp_path, ["app.py"], protected_paths=[])
    report = review_files(files)

    assert report.added_lines == 2
    assert {finding.rule for finding in report.findings} == {"risky_eval"}


def test_review_can_disable_rules() -> None:
    secret_name = "OPENAI_" + "API_KEY"
    secret_value = "sk-" + "secretsecretsecret"
    diff = f"""diff --git a/.env b/.env
--- a/.env
+++ b/.env
@@ -1 +1,2 @@
 APP_ENV=dev
+{secret_name}={secret_value}
"""

    report = review_diff_text(diff, protected_paths=[".env"], disabled_rules=["sensitive_path", "secret_added"])

    assert not report.findings


def test_review_uses_configurable_large_diff_threshold_and_max_findings() -> None:
    file = DiffFile(
        path="src/main.py",
        added=[
            DiffLine(number=1, content="debugger"),
            DiffLine(number=2, content="debugger"),
            DiffLine(number=3, content="debugger"),
        ],
    )

    report = review_files([file], large_diff_threshold=2, max_findings=2)

    assert len(report.findings) == 2
    assert report.findings[0].rule == "large_diff"


def test_review_rules_data_marks_disabled_rules() -> None:
    data = review_rules_data(disabled_rules=["large_diff"], large_diff_threshold=1200, max_findings=25)
    rules = {rule["id"]: rule for rule in data["rules"]}

    assert data["effective_config"]["large_diff_threshold"] == 1200
    assert data["effective_config"]["max_findings"] == 25
    assert rules["large_diff"]["enabled"] is False
    assert rules["secret_added"]["enabled"] is True
