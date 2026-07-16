from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

from app.tools.base import ToolContext, ToolResult, is_protected_path, resolve_workspace_path


MAX_FINDINGS = 50

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
    (re.compile(r"\beval\s*\("), "eval 调用会执行动态代码，容易引入注入风险。", "risky_eval"),
    (re.compile(r"\bexec\s*\("), "exec 调用会执行动态代码，容易引入注入风险。", "risky_exec"),
    (re.compile(r"\bos\.system\s*\("), "os.system 会把字符串交给 shell 执行，请优先使用参数化 subprocess。", "risky_os_system"),
    (re.compile(r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"), "subprocess shell=True 需要额外审查命令注入风险。", "risky_shell_true"),
    (re.compile(r"child_process\.exec\s*\("), "child_process.exec 会通过 shell 执行命令，请确认输入不可被用户控制。", "risky_child_exec"),
    (re.compile(r"verify\s*=\s*False"), "TLS 校验被关闭，请确认只用于测试环境。", "risky_tls_verify"),
]

DEBUG_PATTERNS = [
    re.compile(r"\bconsole\.log\s*\("),
    re.compile(r"\bdebugger\b"),
    re.compile(r"\bprint\s*\("),
    re.compile(r"\bfmt\.Println\s*\("),
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


class ReviewDiffTool:
    name = "review_diff"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        path_filter = str(args["path"]) if args.get("path") else None
        proc = run_review_diff(context.workspace, path_filter)
        if proc.returncode != 0:
            return ToolResult(success=False, error=proc.stderr.strip() or proc.stdout.strip() or "git diff 失败")

        files = parse_unified_diff(proc.stdout)
        files.extend(load_untracked_files(context.workspace, list_untracked_paths(context.workspace, path_filter), context.protected_paths))
        report = review_files(files, protected_paths=context.protected_paths)
        return ToolResult(success=True, text=format_review_report(report), data=review_report_data(report))


def review_diff_text(diff_text: str, protected_paths: Sequence[str] | None = None) -> ReviewReport:
    files = parse_unified_diff(diff_text)
    return review_files(files, protected_paths=protected_paths)


def review_files(files: list[DiffFile], protected_paths: Sequence[str] | None = None) -> ReviewReport:
    protected_paths = protected_paths or []
    findings = collect_findings(files, protected_paths)
    added_lines = sum(len(file.added) for file in files)
    removed_lines = sum(len(file.removed) for file in files)
    return ReviewReport(findings=findings, files=files, added_lines=added_lines, removed_lines=removed_lines)


def run_review_diff(workspace: Path, path_filter: str | None) -> subprocess.CompletedProcess[str]:
    extra = ["--", path_filter] if path_filter else []
    proc = subprocess.run(
        ["git", "diff", "HEAD", *extra],
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if proc.returncode == 0:
        return proc

    if "HEAD" not in proc.stderr:
        return proc

    return subprocess.run(
        ["git", "diff", *extra],
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def list_untracked_paths(workspace: Path, path_filter: str | None) -> list[str]:
    extra = ["--", path_filter] if path_filter else []
    proc = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", *extra],
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
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


def collect_findings(files: list[DiffFile], protected_paths: Sequence[str]) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    seen: set[tuple[str, str, int | None, str]] = set()

    def add(severity: str, path: str, line: int | None, title: str, message: str, rule: str) -> None:
        if len(findings) >= MAX_FINDINGS:
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
                "受保护或敏感路径发生变更",
                "diff 触及受保护路径或常见敏感文件，请确认没有把凭证、生产配置或私钥提交到仓库。",
                "sensitive_path",
            )

        if file.is_deleted and looks_like_test_path(path):
            add(
                "medium",
                path,
                None,
                "测试文件被删除",
                "变更删除了测试文件，请确认有替代覆盖或这是预期清理。",
                "deleted_test",
            )

        for line in file.added:
            if contains_secret(line.content):
                add(
                    "high",
                    path,
                    line.number,
                    "新增行包含疑似密钥",
                    "新增内容匹配凭证或私钥特征，请改用环境变量、密钥管理或测试 fixture。",
                    "secret_added",
                )
            risky = risky_code_message(line.content)
            if risky is not None:
                message, rule = risky
                add("medium", path, line.number, "新增高风险代码模式", message, rule)
            if contains_task_marker(line.content):
                title = "新增 " + "TO" + "DO/FIX" + "ME"
                add("low", path, line.number, title, "新增待办标记可能表示变更尚未完成。", "task_marker_added")
            if contains_debug_statement(line.content) and not looks_like_test_path(path):
                add("low", path, line.number, "新增调试输出", "新增调试输出出现在非测试文件中，请确认不会污染运行日志或用户输出。", "debug_output")

    if total_changed > 500:
        add(
            "medium",
            ".",
            None,
            "diff 规模较大",
            "当前 diff 超过 500 行变更，建议拆分提交或扩大测试覆盖后再合入。",
            "large_diff",
        )

    return sorted(findings, key=lambda item: (SEVERITY_ORDER.get(item.severity, 99), item.path, item.line or 0, item.rule))


def format_review_report(report: ReviewReport) -> str:
    by_severity = severity_counts(report.findings)
    file_count = len(report.files)
    base = f"Review 结果: 已检查 {file_count} 个文件，新增 {report.added_lines} 行，删除 {report.removed_lines} 行。"

    if not report.findings:
        return base + "\n未发现确定性风险。"

    lines = [
        base,
        f"发现 {len(report.findings)} 个问题: high={by_severity['high']} medium={by_severity['medium']} low={by_severity['low']}",
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
