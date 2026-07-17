from __future__ import annotations

import re
from typing import Any


def analyze_test_output(command: str, output: str, returncode: int | None = None) -> dict[str, Any]:
    framework = detect_test_framework(command, output)
    failures = extract_failures(framework, output)
    summary = extract_summary(framework, output, failures, returncode)
    return {
        "framework": framework,
        "summary": summary,
        "failures": failures[:10],
        "failure_count": len(failures),
    }


def detect_test_framework(command: str, output: str) -> str:
    lowered_command = command.lower()
    if "pytest" in lowered_command or "pytest" in output.lower():
        return "pytest"
    if lowered_command.startswith("go test") or "\n--- FAIL:" in output:
        return "go"
    if "jest" in lowered_command or "npm test" in lowered_command or "pnpm test" in lowered_command or "yarn test" in lowered_command:
        return "javascript"
    if "unittest" in lowered_command:
        return "unittest"
    return "unknown"


def extract_failures(framework: str, output: str) -> list[dict[str, Any]]:
    if framework == "pytest":
        return extract_pytest_failures(output)
    if framework == "go":
        return extract_go_failures(output)
    if framework == "javascript":
        return extract_javascript_failures(output)
    if framework == "unittest":
        return extract_unittest_failures(output)
    return []


def extract_pytest_failures(output: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = re.match(r"^(FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$", line.strip())
        if not match:
            continue
        node = match.group(2)
        message = (match.group(3) or "").strip()
        path = node.split("::", 1)[0]
        failures.append({"name": node, "path": path, "message": message})
    return failures


def extract_go_failures(output: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in output.splitlines():
        test_match = re.match(r"^--- FAIL:\s+(\S+)", line.strip())
        if test_match:
            current = {"name": test_match.group(1), "message": ""}
            failures.append(current)
            continue
        location_match = re.match(r"^\s+([^:\s]+\.go):(\d+):\s*(.*)$", line)
        if location_match and current is not None:
            current["path"] = location_match.group(1)
            current["line"] = int(location_match.group(2))
            current["message"] = location_match.group(3).strip()
    return failures


def extract_javascript_failures(output: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    current_path = ""
    for line in output.splitlines():
        stripped = line.strip()
        file_match = re.match(r"^FAIL\s+(.+)$", stripped)
        if file_match:
            current_path = file_match.group(1).strip()
            failures.append({"name": current_path, "path": current_path, "message": ""})
            continue
        test_match = re.match(r"^(?:x|X|-)\s+(.+)$", stripped)
        if test_match and current_path:
            failures.append({"name": test_match.group(1).strip(), "path": current_path, "message": ""})
    return failures


def extract_unittest_failures(output: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = re.match(r"^(FAIL|ERROR):\s+(.+)$", line.strip())
        if match:
            failures.append({"name": match.group(2).strip(), "message": match.group(1).lower()})
    return failures


def extract_summary(framework: str, output: str, failures: list[dict[str, Any]], returncode: int | None) -> str:
    if framework == "pytest":
        for line in reversed(output.splitlines()):
            stripped = line.strip()
            if stripped.startswith("=") and (" failed" in stripped or " error" in stripped):
                return stripped.strip("= ")
    if framework == "go":
        package = first_match(output, r"^FAIL[ \t]+(\S+)", flags=re.MULTILINE)
        if package:
            return f"go test failed in {package}"
    if failures:
        return f"{len(failures)} test failure(s) detected"
    if returncode and returncode != 0:
        return f"test command exited with code {returncode}"
    return ""


def first_match(text: str, pattern: str, flags: int = 0) -> str:
    match = re.search(pattern, text, flags)
    if not match:
        return ""
    return match.group(1).strip()
