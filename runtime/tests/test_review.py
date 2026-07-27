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
    assert "Protected or sensitive path changed" in text
    assert "Added line may contain a secret" in text
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


def test_review_detects_frontend_xss_patterns() -> None:
    diff = """diff --git a/app/view.tsx b/app/view.tsx
--- a/app/view.tsx
+++ b/app/view.tsx
@@ -1 +1,3 @@
 export function View(props) {
+  node.innerHTML = props.html
+  return <div dangerouslySetInnerHTML={{__html: props.html}} />
 }
"""

    report = review_diff_text(diff)
    rules = {finding.rule for finding in report.findings}

    assert "risky_inner_html" in rules
    assert "risky_dangerously_set_inner_html" in rules


def test_review_detects_python_deserialization_patterns() -> None:
    diff = """diff --git a/app/config.py b/app/config.py
--- a/app/config.py
+++ b/app/config.py
@@ -1 +1,3 @@
 def load_config(raw):
+    data = yaml.load(raw)
+    return pickle.loads(raw)
"""

    report = review_diff_text(diff)
    rules = {finding.rule for finding in report.findings}

    assert "risky_yaml_load" in rules
    assert "risky_pickle" in rules


def test_review_detects_go_tls_and_permission_patterns() -> None:
    diff = """diff --git a/server/main.go b/server/main.go
--- a/server/main.go
+++ b/server/main.go
@@ -1 +1,3 @@
 func main() {
+    cfg := &tls.Config{InsecureSkipVerify: true}
+    os.Chmod(path, 0777)
 }
"""

    report = review_diff_text(diff)
    rules = {finding.rule for finding in report.findings}

    assert "risky_go_insecure_tls" in rules
    assert "risky_chmod_777" in rules


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
    assert "No deterministic risks found" in text


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

    assert data["config_warnings"] == []
    assert data["effective_config"]["large_diff_threshold"] == 1200
    assert data["effective_config"]["max_findings"] == 25
    assert rules["large_diff"]["enabled"] is False
    assert rules["secret_added"]["enabled"] is True


def test_review_rules_data_reports_unknown_disabled_rules() -> None:
    data = review_rules_data(disabled_rules=["large_diff", "old_rule"])

    assert data["effective_config"]["disabled_rules"] == ["large_diff", "old_rule"]
    assert data["config_warnings"] == [
        {
            "type": "unknown_disabled_rule",
            "rule": "old_rule",
            "message": "disabledRules contains unknown rule old_rule; this entry has no effect.",
        }
    ]
