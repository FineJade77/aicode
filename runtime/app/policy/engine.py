from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Any


READ_ONLY_TOOLS_V2 = {"read_file", "search", "list_files", "related_files", "review_diff"}
READ_ONLY_MODES = {"review", "commit_message", "explain"}

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
# (`&&`, newlines) — if every sub-command they join is benign the overall
# command can still be "allow" (e.g. "pytest && echo done"). Others
# (`;`, `||`, `|`, bare `&`) can smuggle fallback/secondary/background work
# past review, so their mere presence forces at least "ask" even when every
# sub-command classifies as benign on its own.
# Redirection and command-substitution markers (`>`, `>>`, `<`, `` ` ``,
# `$(`) are not statement separators we can safely split on, but their
# presence also forces at least "ask" since they can hide execution that
# static splitting can't see.
#
# Splitting is done with a quote/escape-aware tokenizer (shlex in
# "punctuation_chars" mode) rather than a raw regex, so a separator
# character that is quoted (`echo "a && b"`) or escaped (`find . -exec rm
# {} \;`) is kept inside its owning word token instead of being mistaken
# for a real statement boundary.
_FLOOR_SEPARATORS = {";", "|", "||", "&"}
_INLINE_ASK_FLOOR_TOKENS = (">>", ">", "<", "$(", "`")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ENV_WRAPPER_NAMES = {"env", "command"}
# Punctuation chars handed to shlex.shlex so it emits `;`, `&`, `&&`, `|`,
# `||`, `(`, `)`, `<`, `>` as their own tokens instead of folding them into
# surrounding words. `\n` is added on top of shlex's own defaults so a bare
# newline (moved out of `whitespace` below) is also emitted as a token,
# letting us tell "ls\nrm -rf /" apart from a single merged word.
_STATEMENT_PUNCTUATION = "();<>|&\n"
# A token is a genuine statement separator only if it is made up entirely
# of these characters (e.g. ";", "&", "&&", "|", "||", "\n"). Tokens like
# "<", ">", "(", ")" are punctuation too but are not statement separators —
# they stay inside the sub-command's token list and are handled by the
# inline-ask-floor substring check instead.
_SEP_OPERATOR_CHARS = set(";&|\n")


def _reason(language: str, zh: str, en: str) -> str:
    """Pick a localized reason string. Defaults to Chinese (the default UI language)."""
    return en if str(language).startswith("en") else zh


@dataclass(slots=True)
class GateDecision:
    verdict: str  # allow | ask | deny
    risk_level: str
    reason: str = ""


class PolicyEngine:
    """Small, conservative policy layer for Phase 1 tool execution."""

    def gate(self, tool_name: str, args: dict[str, Any], mode: str = "default", language: str = "zh-CN") -> GateDecision:
        if tool_name in READ_ONLY_TOOLS_V2:
            return GateDecision("allow", "low")
        if mode in READ_ONLY_MODES:
            return GateDecision(
                "deny",
                "high",
                _reason(language, f"{mode} 模式只允许只读工具", f"{mode} mode only permits read-only tools"),
            )
        if tool_name == "edit_file":
            return GateDecision("ask", "medium", _reason(language, "文件写入需要 inline diff 确认", "file writes require inline diff confirmation"))
        if tool_name == "bash":
            return self.gate_bash(str(args.get("command", "")), language=language)
        return GateDecision("deny", "high", _reason(language, f"未知工具: {tool_name}", f"unknown tool: {tool_name}"))

    def gate_bash(self, command: str, language: str = "zh-CN") -> GateDecision:
        command = command.strip()
        if not command:
            return GateDecision("deny", "low", _reason(language, "空命令", "empty command"))

        ask_floor = any(token in command for token in _INLINE_ASK_FLOOR_TOKENS)

        try:
            segments, sep_floor = self._split_statements(command)
        except ValueError as exc:
            return GateDecision("deny", "high", str(exc))
        ask_floor = ask_floor or sep_floor

        decisions = [self._classify_single(segment, language) for segment in segments]
        decisions = [decision for decision in decisions if decision is not None]

        if not decisions:
            return GateDecision("deny", "low", _reason(language, "空命令", "empty command"))

        for decision in decisions:
            if decision.verdict == "deny":
                return decision

        ask_decision = next((decision for decision in decisions if decision.verdict == "ask"), None)
        if ask_decision is not None:
            return ask_decision
        if ask_floor:
            return GateDecision("ask", "high", _reason(language, "包含 shell 控制符，需要确认后执行", "contains shell control characters; needs confirmation"))
        return GateDecision("allow", "low")

    def _tokenize(self, command: str) -> list[str]:
        """Tokenize a raw command with a quote/escape-aware shell lexer.

        Uses `shlex.shlex` in punctuation-chars mode so operators (`;`,
        `&`, `&&`, `|`, `||`, `<`, `>`, `(`, `)`) come out as their own
        tokens while quoted strings and escaped characters (`\\;`, `"a &&
        b"`) stay intact inside a single word token. `\n` is pulled out of
        `whitespace` and added to `punctuation_chars` so a bare newline is
        also emitted as its own separator token instead of silently
        merging the words on either side of it.
        """
        lexer = shlex.shlex(command, posix=True, punctuation_chars=_STATEMENT_PUNCTUATION)
        lexer.whitespace_split = True
        lexer.whitespace = lexer.whitespace.replace("\n", "")
        return list(lexer)

    def _is_separator_operator(self, token: str) -> bool:
        return bool(token) and all(ch in _SEP_OPERATOR_CHARS for ch in token)

    def _split_statements(self, command: str) -> tuple[list[list[str]], bool]:
        """Split a raw command into sub-command token lists.

        Splits on genuine statement-separator tokens (`;`, `&`, `&&`,
        `|`, `||`, newlines) as identified by a quote/escape-aware
        tokenizer, so a separator hidden inside quotes or escaped (e.g.
        `find . -exec rm {} \\;`, `echo "a && b"`) is kept as part of its
        owning sub-command instead of being mistaken for a boundary.

        Returns the non-empty sub-command token lists plus a flag
        indicating whether an "ask" floor was earned, either because a
        real `;`/`|`/`||` separator was used, or because a sub-command's
        argument text merely *contains* a compound operator look-alike
        (`&&`/`||`) that a quote-aware split proved is not an actual
        separator — such input is provably inert but still suspicious
        enough to require a human look rather than a silent allow.

        Raises ValueError if the command is not valid shell syntax (e.g.
        genuinely unbalanced quotes); the caller treats that as "deny".
        """
        tokens = self._tokenize(command)
        segments: list[list[str]] = []
        current: list[str] = []
        floor = False
        for token in tokens:
            if self._is_separator_operator(token):
                if current:
                    segments.append(current)
                    current = []
                if token in _FLOOR_SEPARATORS:
                    floor = True
                continue
            if "&&" in token or "||" in token:
                floor = True
            current.append(token)
        if current:
            segments.append(current)
        return segments, floor

    def _normalize_parts(self, parts: list[str]) -> list[str]:
        """Strip leading env-assignment tokens and env/command wrappers.

        This keeps the deny/allow lists from being defeated by a prefix
        like `FOO=1 rm -rf /`, `env rm -rf /`, `/usr/bin/env -i rm -rf /`
        or `/usr/bin/env -S rm -rf /` that leaves argv[0] pointing at
        something other than the real executable. The wrapper check is
        basename-based (applied after env-assignment stripping) so a
        path-qualified `env`/`command` invocation is recognized too, and
        any flag tokens immediately following the wrapper (`-i`, `-S`,
        `-u NAME`, ...) are skipped along with it.
        """
        parts = list(parts)
        changed = True
        while changed:
            changed = False
            while parts and _ENV_ASSIGN_RE.match(parts[0]):
                parts = parts[1:]
                changed = True
            if parts and os.path.basename(parts[0]) in _ENV_WRAPPER_NAMES:
                parts = parts[1:]
                changed = True
                while parts and parts[0].startswith("-"):
                    parts = parts[1:]
        return parts

    def _classify_single(self, parts: list[str], language: str = "zh-CN") -> GateDecision | None:
        if not parts:
            return None
        parts = self._normalize_parts(parts)
        if not parts:
            return GateDecision("ask", "medium", _reason(language, "命令需要确认后执行", "command needs confirmation before running"))
        executable = parts[0]
        basename = os.path.basename(executable)
        if executable in DENY_EXECUTABLES or basename in DENY_EXECUTABLES:
            return GateDecision("deny", "high", _reason(language, f"禁止执行高风险命令: {executable}", f"high-risk command is not allowed: {executable}"))
        if executable == "git" or basename == "git":
            return self._gate_git(parts, language)
        if self._is_low_risk_test(parts):
            return GateDecision("allow", "low")
        if executable in ALLOW_EXECUTABLES:
            return GateDecision("allow", "low")
        return GateDecision("ask", "medium", _reason(language, f"命令需要确认后执行: {executable}", f"command needs confirmation before running: {executable}"))

    def _gate_git(self, parts: list[str], language: str = "zh-CN") -> GateDecision:
        subcommand = parts[1] if len(parts) > 1 else ""
        if subcommand in DENY_GIT_SUBCOMMANDS:
            return GateDecision("deny", "high", _reason(language, f"禁止执行破坏性 git 命令: git {subcommand}", f"destructive git command is not allowed: git {subcommand}"))
        if subcommand == "checkout" and "--" in parts:
            return GateDecision("deny", "high", _reason(language, "禁止 git checkout -- 丢弃改动", "git checkout -- (discarding changes) is not allowed"))
        if subcommand == "push" and any(flag in parts for flag in ("--force", "-f", "--force-with-lease", "--delete")):
            return GateDecision("deny", "high", _reason(language, "禁止强制/删除式 git push", "force/delete git push is not allowed"))
        if subcommand == "branch" and any(flag in parts for flag in ("-D", "-d", "-M", "-m")):
            return GateDecision("deny", "high", _reason(language, "禁止删除/重命名分支", "deleting/renaming branches is not allowed"))
        if subcommand == "stash" and "drop" in parts:
            return GateDecision("deny", "high", _reason(language, "禁止 git stash drop", "git stash drop is not allowed"))
        if subcommand in ALLOW_GIT_SUBCOMMANDS:
            return GateDecision("allow", "low")
        return GateDecision("ask", "medium", _reason(language, f"git {subcommand} 需要确认后执行", f"git {subcommand} needs confirmation before running"))

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
