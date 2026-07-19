from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    risk_level: str
    requires_approval: bool
    reason: str = ""


READ_ONLY_TOOLS_V2 = {"read_file", "search", "list_files", "review_diff"}

DENY_EXECUTABLES = {"rm", "sudo", "su", "shutdown", "reboot", "mkfs", "dd"}
ALLOW_EXECUTABLES = {"pwd", "ls", "rg", "grep", "head", "tail", "wc", "cat", "which", "echo"}
ALLOW_GIT_SUBCOMMANDS = {"status", "diff", "show", "log", "blame", "rev-parse"}
DENY_GIT_SUBCOMMANDS = {"reset", "clean", "rebase"}
CONTROL_TOKENS = {"|", "&&", "||", ";", ">", ">>", "<", "$(", "`"}

# --- gate_bash internals -------------------------------------------------
#
# A bash command can smuggle a dangerous statement past classification by
# hiding it after a shell statement separator (`;`, `&&`, `||`, `&`, `|`,
# a newline) or behind an env-assignment / path prefix on argv[0]. To stay
# robust against these bypasses we split the raw command into sub-commands
# on every statement separator BEFORE classifying, classify each
# sub-command independently, and then combine verdicts with "most
# restrictive wins" (deny > ask > allow).
#
# Some separators are considered safe enough for pure control-flow chaining
# (`&&`, bare `&`, newlines) — if every sub-command they join is benign the
# overall command can still be "allow" (e.g. "pytest && echo done").
# Others (`;`, `||`, `|`) are common vectors for smuggling a fallback/
# secondary command past review, so their mere presence forces at least
# "ask" even when every sub-command classifies as benign on its own.
# Redirection and command-substitution markers (`>`, `>>`, `<`, `` ` ``,
# `$(`) are not statement separators we can safely split on, but their
# presence also forces at least "ask" since they can hide execution that
# static splitting can't see.
_ASK_FLOOR_SEPARATORS = {";", "||", "|"}
_INLINE_ASK_FLOOR_TOKENS = (">>", ">", "<", "$(", "`")
_STATEMENT_SEP_RE = re.compile(r"(&&|\|\||[;&|\r\n])")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(slots=True)
class GateDecision:
    verdict: str  # allow | ask | deny
    risk_level: str
    reason: str = ""


class PolicyEngine:
    """Small, conservative policy layer for Phase 1 tool execution."""

    def gate(self, tool_name: str, args: dict[str, Any], mode: str = "default") -> GateDecision:
        if tool_name in READ_ONLY_TOOLS_V2:
            return GateDecision("allow", "low")
        if mode == "review":
            return GateDecision("deny", "high", "review 模式只允许只读工具")
        if tool_name == "edit_file":
            return GateDecision("ask", "medium", "文件写入需要 inline diff 确认")
        if tool_name == "bash":
            return self.gate_bash(str(args.get("command", "")))
        return GateDecision("deny", "high", f"未知工具: {tool_name}")

    def gate_bash(self, command: str) -> GateDecision:
        command = command.strip()
        if not command:
            return GateDecision("deny", "low", "空命令")

        ask_floor = any(token in command for token in _INLINE_ASK_FLOOR_TOKENS)

        segments, sep_floor = self._split_statements(command)
        ask_floor = ask_floor or sep_floor

        decisions = [self._classify_single(segment) for segment in segments]
        decisions = [decision for decision in decisions if decision is not None]

        if not decisions:
            return GateDecision("deny", "low", "空命令")

        for decision in decisions:
            if decision.verdict == "deny":
                return decision

        ask_decision = next((decision for decision in decisions if decision.verdict == "ask"), None)
        if ask_decision is not None:
            return ask_decision
        if ask_floor:
            return GateDecision("ask", "high", "包含 shell 控制符，需要确认后执行")
        return GateDecision("allow", "low")

    def _split_statements(self, command: str) -> tuple[list[str], bool]:
        """Split a raw command on shell statement separators.

        Returns the non-empty sub-command strings plus a flag indicating
        whether any separator that forces an "ask" floor (`;`, `||`, `|`)
        was encountered.
        """
        tokens = _STATEMENT_SEP_RE.split(command)
        segments: list[str] = []
        floor = False
        for index, token in enumerate(tokens):
            if index % 2 == 0:
                if token.strip():
                    segments.append(token)
            elif token in _ASK_FLOOR_SEPARATORS:
                floor = True
        return segments, floor

    def _normalize_parts(self, parts: list[str]) -> list[str]:
        """Strip leading env-assignment tokens and env/command wrappers.

        This keeps the deny/allow lists from being defeated by a prefix
        like `FOO=1 rm -rf /` or `env rm -rf /` that leaves argv[0]
        pointing at something other than the real executable.
        """
        parts = list(parts)
        changed = True
        while changed:
            changed = False
            while parts and _ENV_ASSIGN_RE.match(parts[0]):
                parts = parts[1:]
                changed = True
            if parts and parts[0] in ("env", "command"):
                parts = parts[1:]
                changed = True
        return parts

    def _classify_single(self, command: str) -> GateDecision | None:
        command = command.strip()
        if not command:
            return None
        try:
            raw_parts = shlex.split(command)
        except ValueError as exc:
            return GateDecision("deny", "high", str(exc))
        if not raw_parts:
            return None
        parts = self._normalize_parts(raw_parts)
        if not parts:
            return GateDecision("ask", "medium", "命令需要确认后执行")
        executable = parts[0]
        basename = os.path.basename(executable)
        if executable in DENY_EXECUTABLES or basename in DENY_EXECUTABLES:
            return GateDecision("deny", "high", f"禁止执行高风险命令: {executable}")
        if executable == "git":
            return self._gate_git(parts)
        if self._is_low_risk_test(parts):
            return GateDecision("allow", "low")
        if executable in ALLOW_EXECUTABLES:
            return GateDecision("allow", "low")
        return GateDecision("ask", "medium", f"命令需要确认后执行: {executable}")

    def _gate_git(self, parts: list[str]) -> GateDecision:
        subcommand = parts[1] if len(parts) > 1 else ""
        if subcommand in DENY_GIT_SUBCOMMANDS:
            return GateDecision("deny", "high", f"禁止执行破坏性 git 命令: git {subcommand}")
        if subcommand == "checkout" and "--" in parts:
            return GateDecision("deny", "high", "禁止 git checkout -- 丢弃改动")
        if subcommand == "push" and any(flag in parts for flag in ("--force", "-f", "--force-with-lease", "--delete")):
            return GateDecision("deny", "high", "禁止强制/删除式 git push")
        if subcommand == "branch" and any(flag in parts for flag in ("-D", "-d", "-M", "-m")):
            return GateDecision("deny", "high", "禁止删除/重命名分支")
        if subcommand == "stash" and "drop" in parts:
            return GateDecision("deny", "high", "禁止 git stash drop")
        if subcommand in ALLOW_GIT_SUBCOMMANDS:
            return GateDecision("allow", "low")
        return GateDecision("ask", "medium", f"git {subcommand} 需要确认后执行")

    read_tools = {
        "detect_project",
        "find_files",
        "list_files",
        "read_file",
        "search_text",
        "git_status",
        "git_diff",
        "git_show",
        "review_diff",
    }

    blocked_shell_commands = {
        "rm",
        "sudo",
        "su",
        "shutdown",
        "reboot",
        "mkfs",
        "dd",
    }

    medium_shell_commands = {
        "npm",
        "pnpm",
        "yarn",
        "pip",
        "python",
        "python3",
        "go",
        "docker",
        "chmod",
        "mv",
        "cp",
    }

    low_shell_commands = {
        "pwd",
        "ls",
        "rg",
        "grep",
        "cat",
        "sed",
        "head",
        "tail",
        "git",
        "pytest",
        "go",
        "npm",
        "pnpm",
        "yarn",
        "python",
        "python3",
    }

    shell_control_tokens = {"|", "&&", "||", ";", ">", ">>", "<", "$(", "`"}

    def evaluate(self, tool_name: str, args: dict[str, Any], mode: str = "default") -> PolicyDecision:
        if mode == "review" and tool_name not in self.read_tools:
            return PolicyDecision(
                allowed=False,
                risk_level="high",
                requires_approval=False,
                reason="review 模式只允许只读工具",
            )

        if tool_name in self.read_tools:
            return PolicyDecision(allowed=True, risk_level="low", requires_approval=False)

        if tool_name == "run_shell":
            return self._evaluate_shell(str(args.get("command", "")), mode=mode)

        if tool_name == "run_tests":
            if mode == "review":
                return PolicyDecision(allowed=False, risk_level="high", requires_approval=False, reason="review 模式禁止运行测试")
            return PolicyDecision(allowed=True, risk_level="low", requires_approval=False)

        if tool_name in {"generate_patch", "apply_patch"}:
            return PolicyDecision(allowed=False, risk_level="medium", requires_approval=True, reason="Phase 1 需要 inline diff 确认")

        return PolicyDecision(allowed=False, risk_level="high", requires_approval=False, reason=f"未知工具: {tool_name}")

    def _evaluate_shell(self, command: str, mode: str) -> PolicyDecision:
        if mode == "review":
            return PolicyDecision(allowed=False, risk_level="high", requires_approval=False, reason="review 模式禁止 shell")

        command = command.strip()
        if not command:
            return PolicyDecision(allowed=False, risk_level="low", requires_approval=False, reason="空命令")

        if any(token in command for token in self.shell_control_tokens):
            return PolicyDecision(
                allowed=False,
                risk_level="high",
                requires_approval=True,
                reason="shell 控制符需要显式确认，当前阶段暂不自动执行",
            )

        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return PolicyDecision(allowed=False, risk_level="high", requires_approval=False, reason=str(exc))

        if not parts:
            return PolicyDecision(allowed=False, risk_level="low", requires_approval=False, reason="空命令")

        executable = parts[0]
        if executable in self.blocked_shell_commands:
            return PolicyDecision(allowed=False, risk_level="high", requires_approval=False, reason=f"禁止执行高风险命令: {executable}")

        if executable == "git" and self._is_destructive_git(parts):
            return PolicyDecision(allowed=False, risk_level="high", requires_approval=False, reason="禁止执行破坏性 git 命令")

        if self._is_low_risk_test(parts):
            return PolicyDecision(allowed=True, risk_level="low", requires_approval=False)

        if executable in self.low_shell_commands and executable not in self.medium_shell_commands:
            return PolicyDecision(allowed=True, risk_level="low", requires_approval=False)

        if executable == "git":
            return PolicyDecision(allowed=True, risk_level="low", requires_approval=False)

        return PolicyDecision(
            allowed=False,
            risk_level="medium",
            requires_approval=True,
            reason=f"命令需要确认后执行: {executable}",
        )

    def _is_destructive_git(self, parts: list[str]) -> bool:
        if len(parts) < 2:
            return False
        subcommand = parts[1]
        if subcommand == "reset":
            return True
        if subcommand == "checkout" and "--" in parts:
            return True
        if subcommand in {"clean", "rebase"}:
            return True
        return False

    def _is_low_risk_test(self, parts: list[str]) -> bool:
        if parts[0] == "pytest":
            return True
        if parts[0] == "go" and len(parts) >= 2 and parts[1] == "test":
            return True
        if parts[0] in {"npm", "pnpm", "yarn"} and "test" in parts[1:]:
            return True
        if parts[0] in {"python", "python3"} and len(parts) >= 3 and parts[1] == "-m" and parts[2] in {"pytest", "unittest"}:
            return True
        return False
