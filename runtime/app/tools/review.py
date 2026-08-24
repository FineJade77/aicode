from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.tools.base import ToolContext, ToolResult, is_protected_path, resolve_workspace_path
from app.tools.command import CommandResult, run_command

DEFAULT_MAX_FINDINGS = 50
DEFAULT_LARGE_DIFF_THRESHOLD = 500

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
    re.compile(
        r"\b(api[_-]?key|access[_-]?token|auth[_-]?token|bearer[_-]?token|password|passwd|private[_-]?key|secret)\b"
        r"\s*[:=]\s*['\"]?[^'\"\s]{8,}",
        re.IGNORECASE,
    ),
]

RISKY_CODE_PATTERNS = [
    (re.compile(r"\beval\s*\("), "eval executes dynamic code and can introduce injection vulnerabilities.", "risky_eval"),
    (re.compile(r"\bexec\s*\("), "exec executes dynamic code and can introduce injection vulnerabilities.", "risky_exec"),
    (re.compile(r"\bos\.system\s*\("), "os.system passes a string to a shell; prefer a parameterized subprocess.", "risky_os_system"),
    (re.compile(r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"), "subprocess shell=True requires additional command-injection review.", "risky_shell_true"),
    (re.compile(r"child_process\.exec\s*\("), "child_process.exec runs through a shell; verify that users cannot control its input.", "risky_child_exec"),
    (re.compile(r"verify\s*=\s*False"), "TLS verification is disabled; verify that this is limited to tests.", "risky_tls_verify"),
    (re.compile(r"\.innerHTML\s*="), "Direct innerHTML assignment can introduce XSS; prefer safe rendering or explicit sanitization.", "risky_inner_html"),
    (re.compile(r"\bdangerouslySetInnerHTML\b"), "Verify that React dangerouslySetInnerHTML input is sanitized.", "risky_dangerously_set_inner_html"),
    (re.compile(r"\byaml\.load\s*\((?![^)]*(SafeLoader|safe_load))"), "yaml.load can construct arbitrary objects; use yaml.safe_load or SafeLoader.", "risky_yaml_load"),
    (re.compile(r"\bpickle\.(load|loads)\s*\("), "Deserializing untrusted pickle input can execute arbitrary code; use a safe format.", "risky_pickle"),
    (re.compile(r"\bInsecureSkipVerify\s*:\s*true\b"), "Go TLS InsecureSkipVerify skips certificate validation; verify that this is limited to tests.", "risky_go_insecure_tls"),
    (re.compile(r"\bchmod\s+777\b|os\.Chmod\([^,]+,\s*0?777\)"), "chmod 777 grants overly broad permissions; use least privilege.", "risky_chmod_777"),
]

DEBUG_PATTERNS = [
    re.compile(r"\bconsole\.log\s*\("),
    re.compile(r"\bdebugger\b"),
    re.compile(r"\bpdb\.set_trace\s*\("),
]


@dataclass(slots=True)
class DiffLine:
    number: int | None
    content: str


@dataclass(slots=True)
class DiffFile:
    path: str
    old_path: str = ""
    added: list[DiffLine] = field(default_factory=list)
    removed: list[DiffLine] = field(default_factory=list)
    is_binary: bool = False
    is_deleted: bool = False
    is_new: bool = False


@dataclass(slots=True)
class ReviewFinding:
    severity: str
    path: str
    line: int | None
    title: str
    message: str
    rule: str


@dataclass(slots=True)
class ReviewReport:
    findings: list[ReviewFinding]
    files: list[DiffFile]
    added_lines: int
    removed_lines: int


@dataclass(slots=True)
class ReviewRule:
    rule_id: str
    severity: str
    title: str
    description: str


REVIEW_RULES = [
    ReviewRule(
        rule_id="sensitive_path",
        severity="high",
        title="Protected or sensitive path changed",
        description="The diff touches a protected path, production configuration, credential file, or private key.",
    ),
    ReviewRule(
        rule_id="secret_added",
        severity="high",
        title="Added line may contain a secret",
        description="Added content matches an API key, token, password, private key, or similar credential pattern.",
    ),
    ReviewRule(
        rule_id="deleted_test",
        severity="medium",
        title="Test file deleted",
        description="A test file was deleted; verify replacement coverage or that the deletion is intentional.",
    ),
    ReviewRule(
        rule_id="risky_eval",
        severity="medium",
        title="eval call added",
        description="eval executes dynamic code and can introduce injection vulnerabilities.",
    ),
    ReviewRule(
        rule_id="risky_exec",
        severity="medium",
        title="exec call added",
        description="exec executes dynamic code and can introduce injection vulnerabilities.",
    ),
    ReviewRule(
        rule_id="risky_os_system",
        severity="medium",
        title="os.system call added",
        description="os.system passes a string to a shell; prefer a parameterized subprocess.",
    ),
    ReviewRule(
        rule_id="risky_shell_true",
        severity="medium",
        title="subprocess shell=True added",
        description="subprocess shell=True requires additional command-injection review.",
    ),
    ReviewRule(
        rule_id="risky_child_exec",
        severity="medium",
        title="child_process.exec added",
        description="child_process.exec runs through a shell; verify that users cannot control its input.",
    ),
    ReviewRule(
        rule_id="risky_tls_verify",
        severity="medium",
        title="TLS verification disabled",
        description="TLS verification is disabled; verify that this is limited to tests.",
    ),
    ReviewRule(
        rule_id="risky_inner_html",
        severity="medium",
        title="Direct innerHTML assignment",
        description="Direct innerHTML assignment can introduce XSS; verify that the content is sanitized.",
    ),
    ReviewRule(
        rule_id="risky_dangerously_set_inner_html",
        severity="medium",
        title="dangerouslySetInnerHTML used",
        description="Verify that React dangerouslySetInnerHTML input is sanitized.",
    ),
    ReviewRule(
        rule_id="risky_yaml_load",
        severity="medium",
        title="Unsafe YAML loading",
        description="yaml.load can construct arbitrary objects; use yaml.safe_load or SafeLoader.",
    ),
    ReviewRule(
        rule_id="risky_pickle",
        severity="medium",
        title="Unsafe pickle deserialization",
        description="Deserializing untrusted pickle input can execute arbitrary code.",
    ),
    ReviewRule(
        rule_id="risky_go_insecure_tls",
        severity="medium",
        title="Go TLS certificate verification skipped",
        description="InsecureSkipVerify skips certificate validation; verify that this is limited to tests.",
    ),
    ReviewRule(
        rule_id="risky_chmod_777",
        severity="medium",
        title="Overly broad file permissions",
        description="chmod 777 grants overly broad permissions; use least privilege.",
    ),
    ReviewRule(
        rule_id="large_diff",
        severity="medium",
        title="Large diff",
        description="The diff exceeds the configured threshold; split the change or expand test coverage before merging.",
    ),
    ReviewRule(
        rule_id="task_marker_added",
        severity="low",
        title="TODO/FIXME added",
        description="A new task marker may indicate that the change is incomplete.",
    ),
    ReviewRule(
        rule_id="debug_output",
        severity="low",
        title="Debug output added",
        description="A console.log, debugger, pdb.set_trace, or similar debug artifact was added.",
    ),
]


class ReviewDiffTool:
    name = "review_diff"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        path_filter = str(args["path"]) if args.get("path") else None
        metadata = {
            "mode": context.mode,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "tool_call_id": context.tool_call_id,
            "masked_paths": context.protected_paths,
            "trust_level": context.trust_level,
        }
        proc = await run_review_diff(context.workspace, path_filter, execution=context.execution, metadata=metadata)
        if proc.returncode != 0:
            return ToolResult(success=False, error=proc.stderr.strip() or proc.stdout.strip() or "git diff failed")

        files = parse_unified_diff(proc.stdout)
        files.extend(
            load_untracked_files(
                context.workspace,
                await list_untracked_paths(
                    context.workspace,
                    path_filter,
                    execution=context.execution,
                    metadata=metadata,
                ),
                context.protected_paths,
            )
        )
        report = review_files(
            files,
            protected_paths=context.protected_paths,
            disabled_rules=context.review_disabled_rules,
            large_diff_threshold=context.review_large_diff_threshold,
            max_findings=context.review_max_findings,
        )
        return ToolResult(success=True, text=format_review_report(report), data=review_report_data(report))


def review_diff_text(
    diff_text: str,
    protected_paths: Sequence[str] | None = None,
    disabled_rules: Sequence[str] | None = None,
    large_diff_threshold: int = DEFAULT_LARGE_DIFF_THRESHOLD,
    max_findings: int = DEFAULT_MAX_FINDINGS,
) -> ReviewReport:
    files = parse_unified_diff(diff_text)
    return review_files(
        files,
        protected_paths=protected_paths,
        disabled_rules=disabled_rules,
        large_diff_threshold=large_diff_threshold,
        max_findings=max_findings,
    )


def review_files(
    files: list[DiffFile],
    protected_paths: Sequence[str] | None = None,
    disabled_rules: Sequence[str] | None = None,
    large_diff_threshold: int = DEFAULT_LARGE_DIFF_THRESHOLD,
    max_findings: int = DEFAULT_MAX_FINDINGS,
) -> ReviewReport:
    protected_paths = protected_paths or []
    findings = collect_findings(
        files,
        protected_paths,
        disabled_rules=disabled_rules,
        large_diff_threshold=large_diff_threshold,
        max_findings=max_findings,
    )
    added_lines = sum(len(file.added) for file in files)
    removed_lines = sum(len(file.removed) for file in files)
    return ReviewReport(findings=findings, files=files, added_lines=added_lines, removed_lines=removed_lines)


async def run_review_diff(
    workspace: Path,
    path_filter: str | None,
    *,
    execution=None,
    metadata: dict | None = None,
) -> CommandResult:
    extra = ["--", path_filter] if path_filter else []
    command_metadata = {**(metadata or {}), "action": "review.diff"}
    proc = await run_command(
        ["git", "diff", "HEAD", *extra],
        cwd=workspace,
        timeout=20,
        execution=execution,
        metadata=command_metadata,
    )
    if proc.returncode == 0:
        return proc

    if "HEAD" not in proc.stderr:
        return proc

    return await run_command(
        ["git", "diff", *extra],
        cwd=workspace,
        timeout=20,
        execution=execution,
        metadata=command_metadata,
    )


async def list_untracked_paths(
    workspace: Path,
    path_filter: str | None,
    *,
    execution=None,
    metadata: dict | None = None,
) -> list[str]:
    extra = ["--", path_filter] if path_filter else []
    proc = await run_command(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", *extra],
        cwd=workspace,
        timeout=20,
        execution=execution,
        metadata={**(metadata or {}), "action": "review.untracked"},
    )
    if proc.returncode != 0 or not proc.stdout:
        return []
    return [path for path in proc.stdout.split("\0") if path]


def load_untracked_files(workspace: Path, paths: Sequence[str], protected_paths: Sequence[str]) -> list[DiffFile]:
    files: list[DiffFile] = []
    for raw_path in paths:
        path = raw_path.replace("\\", "/")
        file = DiffFile(path=path, old_path="/dev/null", is_new=True)
        files.append(file)

        if is_sensitive_path(path, protected_paths):
            continue

        candidate = resolve_workspace_path(workspace, path)
        if not candidate.is_file():
            continue

        try:
            raw = candidate.read_bytes()
        except OSError:
            continue

        if b"\0" in raw:
            file.is_binary = True
            continue

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            file.is_binary = True
            continue

        for line_number, line in enumerate(text.splitlines(), start=1):
            file.added.append(DiffLine(number=line_number, content=line))

    return files


def parse_unified_diff(diff_text: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current: DiffFile | None = None
    old_line: int | None = None
    new_line: int | None = None

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            current = DiffFile(path=parse_diff_git_path(raw_line), old_path=parse_diff_git_old_path(raw_line))
            files.append(current)
            old_line = None
            new_line = None
            continue

        if current is None:
            continue

        if raw_line.startswith("new file mode"):
            current.is_new = True
            continue
        if raw_line.startswith("deleted file mode"):
            current.is_deleted = True
            continue
        if raw_line.startswith("Binary files "):
            current.is_binary = True
            continue
        if raw_line.startswith("--- "):
            old_path = normalize_diff_path(raw_line[4:])
            if old_path != "/dev/null":
                current.old_path = old_path
            continue
        if raw_line.startswith("+++ "):
            new_path = normalize_diff_path(raw_line[4:])
            if new_path != "/dev/null":
                current.path = new_path
            continue

        hunk = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
        if hunk:
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(2))
            continue

        if raw_line.startswith("\\ No newline"):
            continue
        if old_line is None or new_line is None:
            continue

        if raw_line.startswith("+"):
            current.added.append(DiffLine(number=new_line, content=raw_line[1:]))
            new_line += 1
        elif raw_line.startswith("-"):
            current.removed.append(DiffLine(number=old_line, content=raw_line[1:]))
            old_line += 1
        else:
            old_line += 1
            new_line += 1

    return files


def collect_findings(
    files: list[DiffFile],
    protected_paths: Sequence[str],
    *,
    disabled_rules: Sequence[str] | None = None,
    large_diff_threshold: int = DEFAULT_LARGE_DIFF_THRESHOLD,
    max_findings: int = DEFAULT_MAX_FINDINGS,
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    seen: set[tuple[str, str, int | None, str]] = set()
    disabled = {str(rule) for rule in disabled_rules or []}

    def add(severity: str, path: str, line: int | None, title: str, message: str, rule: str) -> None:
        if rule in disabled:
            return
        key = (rule, path, line, severity)
        if key in seen:
            return
        seen.add(key)
        findings.append(ReviewFinding(severity=severity, path=path, line=line, title=title, message=message, rule=rule))

    total_changed = 0
    for file in files:
        total_changed += len(file.added) + len(file.removed)
        path = file.path or file.old_path

        if is_sensitive_path(path, protected_paths):
            add(
                "high",
                path,
                None,
                "Protected or sensitive path changed",
                "The diff touches a protected path or common sensitive file; verify that no credential, production configuration, or private key is committed.",
                "sensitive_path",
            )

        if file.is_deleted and looks_like_test_path(path):
            add(
                "medium",
                path,
                None,
                "Test file deleted",
                "A test file was deleted; verify replacement coverage or that the deletion is intentional.",
                "deleted_test",
            )

        for line in file.added:
            if contains_secret(line.content):
                add(
                    "high",
                    path,
                    line.number,
                    "Added line may contain a secret",
                    "Added content matches a credential or private-key pattern; use environment variables, secret management, or a test fixture.",
                    "secret_added",
                )
            risky = risky_code_message(line.content)
            if risky is not None:
                message, rule = risky
                add("medium", path, line.number, "High-risk code pattern added", message, rule)
            if contains_task_marker(line.content):
                title = "Added " + "TO" + "DO/FIX" + "ME"
                add("low", path, line.number, title, "A new task marker may indicate that the change is incomplete.", "task_marker_added")
            if contains_debug_statement(line.content) and not looks_like_test_path(path):
                add("low", path, line.number, "Debug output added", "Debug output was added outside a test file; verify that it will not pollute logs or user output.", "debug_output")

    if large_diff_threshold > 0 and total_changed > large_diff_threshold:
        add(
            "medium",
            ".",
            None,
            "Large diff",
            "The diff exceeds 500 changed lines; split the change or expand test coverage before merging.",
            "large_diff",
        )

    sorted_findings = sorted(findings, key=lambda item: (SEVERITY_ORDER.get(item.severity, 99), item.path, item.line or 0, item.rule))
    return sorted_findings[: max(1, max_findings)]


def format_review_report(report: ReviewReport) -> str:
    by_severity = severity_counts(report.findings)
    file_count = len(report.files)
    base = f"Review result: checked {file_count} files, with {report.added_lines} lines added and {report.removed_lines} lines removed."

    if not report.findings:
        return base + "\nNo deterministic risks found."

    lines = [
        base,
        f"Found {len(report.findings)} issues: high={by_severity['high']} medium={by_severity['medium']} low={by_severity['low']}",
        "",
    ]
    for finding in report.findings:
        location = finding.path
        if finding.line is not None:
            location += f":{finding.line}"
        lines.append(f"[{finding.severity}] {location} {finding.title}")
        lines.append(f"  {finding.message}")
    return "\n".join(lines)


def review_report_data(report: ReviewReport) -> dict:
    by_severity = severity_counts(report.findings)
    return {
        "summary": {
            "files": len(report.files),
            "added_lines": report.added_lines,
            "removed_lines": report.removed_lines,
            "finding_count": len(report.findings),
            "by_severity": by_severity,
        },
        "findings": [asdict(finding) for finding in report.findings],
    }


def review_rules_data(
    *,
    disabled_rules: Sequence[str] | None = None,
    large_diff_threshold: int = DEFAULT_LARGE_DIFF_THRESHOLD,
    max_findings: int = DEFAULT_MAX_FINDINGS,
) -> dict:
    disabled = {str(rule) for rule in disabled_rules or []}
    known_rules = {rule.rule_id for rule in REVIEW_RULES}
    unknown_disabled_rules = sorted(disabled - known_rules)
    return {
        "effective_config": {
            "disabled_rules": sorted(disabled),
            "large_diff_threshold": large_diff_threshold,
            "max_findings": max_findings,
        },
        "defaults": {
            "large_diff_threshold": DEFAULT_LARGE_DIFF_THRESHOLD,
            "max_findings": DEFAULT_MAX_FINDINGS,
        },
        "config_warnings": [
            {
                "type": "unknown_disabled_rule",
                "rule": rule,
                "message": f"disabledRules contains unknown rule {rule}; this entry has no effect.",
            }
            for rule in unknown_disabled_rules
        ],
        "rules": [
            {
                "id": rule.rule_id,
                "severity": rule.severity,
                "enabled": rule.rule_id not in disabled,
                "title": rule.title,
                "description": rule.description,
            }
            for rule in REVIEW_RULES
        ],
    }


def severity_counts(findings: Sequence[ReviewFinding]) -> dict[str, int]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        if finding.severity in counts:
            counts[finding.severity] += 1
    return counts


def is_sensitive_path(path: str, protected_paths: Sequence[str]) -> bool:
    normalized = path.lower().replace("\\", "/")
    if is_protected_path(path, list(protected_paths)):
        return True
    sensitive_markers = [
        ".env",
        ".pem",
        ".key",
        "id_rsa",
        "id_dsa",
        "secret",
        "secrets/",
        "credential",
        "credentials/",
        "private_key",
    ]
    return any(marker in normalized for marker in sensitive_markers)


def looks_like_test_path(path: str) -> bool:
    normalized = path.lower().replace("\\", "/")
    return (
        "/test/" in normalized
        or "/tests/" in normalized
        or normalized.startswith("test/")
        or normalized.startswith("tests/")
        or normalized.endswith("_test.go")
        or normalized.endswith(".test.ts")
        or normalized.endswith(".test.tsx")
        or normalized.endswith(".spec.ts")
        or normalized.endswith(".spec.tsx")
        or normalized.endswith("_test.py")
    )


def contains_secret(line: str) -> bool:
    return any(pattern.search(line) for pattern in SECRET_PATTERNS)


def risky_code_message(line: str) -> tuple[str, str] | None:
    for pattern, message, rule in RISKY_CODE_PATTERNS:
        if pattern.search(line):
            return message, rule
    return None


def contains_task_marker(line: str) -> bool:
    upper = line.upper()
    markers = ("TO" + "DO", "FIX" + "ME")
    return any(marker in upper for marker in markers)


def contains_debug_statement(line: str) -> bool:
    return any(pattern.search(line) for pattern in DEBUG_PATTERNS)


def normalize_diff_path(raw_path: str) -> str:
    path = raw_path.strip()
    if "\t" in path:
        path = path.split("\t", 1)[0]
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def parse_diff_git_path(line: str) -> str:
    match = re.match(r"diff --git a/(.*) b/(.*)", line)
    if match:
        return match.group(2)
    return "."


def parse_diff_git_old_path(line: str) -> str:
    match = re.match(r"diff --git a/(.*) b/(.*)", line)
    if match:
        return match.group(1)
    return "."
