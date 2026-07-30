from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.paths import is_protected_path
from app.core.tools import ToolSpec
from app.security.secrets import contains_known_environment_secret

# Modes in which no write tool may run, whatever the schema exposed. This is the
# hard enforcement layer for the case where a client bypasses the tool schema.
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
_SHELL_WRAPPERS = {"sh", "bash", "zsh", "dash", "fish"}
_SYSTEM_EXECUTABLE_ROOTS = tuple(Path(value).resolve() for value in ("/bin", "/usr/bin"))
_SENSITIVE_PATH_MARKERS = {
    ".env",
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".docker",
    ".kube",
    ".netrc",
    ".git-credentials",
    "credentials",
    "id_rsa",
    "id_ed25519",
    "private_key",
    "service-account",
    "gcloud",
}


@dataclass(slots=True)
class GateDecision:
    verdict: str  # allow | ask | deny
    risk_level: str
    reason: str = ""


class PolicyEngine:
    """Small, conservative policy layer for Phase 1 tool execution."""

    def gate(
        self,
        tool_name: str,
        args: dict[str, Any],
        mode: str = "default",
        *,
        spec: ToolSpec | None = None,
        workspace: Path | None = None,
        protected_paths: list[str] | None = None,
        trust_level: str = "trusted",
    ) -> GateDecision:
        """Classify a tool call from the tool's own declaration.

        The engine reads `read_only` and `approval` off the `ToolSpec` rather
        than keeping its own copy of which tools are which; that copy previously
        had to be kept in step with the registry by hand.

        A missing spec means the tool is not registered, which stays a hard deny:
        anything the Runtime cannot describe must not run.
        """
        if spec is None:
            return GateDecision("deny", "high", f"unknown tool: {tool_name}")
        if spec.read_only:
            return GateDecision("allow", "low")
        if mode in READ_ONLY_MODES:
            return GateDecision(
                "deny",
                "high",
                f"{mode} mode only permits read-only tools",
            )
        if spec.approval == "diff":
            return GateDecision("ask", "medium", "file writes require inline diff confirmation")
        if tool_name == "bash":
            return self.gate_bash(
                str(args.get("command", "")),
                workspace=workspace,
                protected_paths=protected_paths,
                trust_level=trust_level,
            )
        # A registered non-read-only tool that is neither an edit nor a shell
        # command: an externally provided tool such as one exposed over MCP. The
        # Runtime cannot reason about what it does, so it always asks.
        return GateDecision("ask", "medium", f"{tool_name} is not a read-only tool and needs confirmation")
    def gate_bash(
        self,
        command: str,
        *,
        workspace: Path | None = None,
        protected_paths: list[str] | None = None,
        trust_level: str = "trusted",
    ) -> GateDecision:
        command = command.strip()
        if not command:
            return GateDecision("deny", "low", "empty command")
        if contains_known_environment_secret(command):
            return GateDecision(
                "deny",
                "high",
                "command contains a sensitive Runtime environment value",
            )

        ask_floor = any(token in command for token in _INLINE_ASK_FLOOR_TOKENS)

        try:
            segments, sep_floor = self._split_statements(command)
        except ValueError as exc:
            return GateDecision("deny", "high", str(exc))
        ask_floor = ask_floor or sep_floor

        path_decision = self._gate_paths(
            segments,
            workspace=workspace,
            protected_paths=protected_paths or [],
        )
        if path_decision is not None:
            return path_decision

        decisions = [self._classify_single(segment, trust_level=trust_level) for segment in segments]
        decisions = [decision for decision in decisions if decision is not None]

        if not decisions:
            return GateDecision("deny", "low", "empty command")

        for decision in decisions:
            if decision.verdict == "deny":
                return decision

        ask_decision = next((decision for decision in decisions if decision.verdict == "ask"), None)
        if ask_decision is not None:
            return ask_decision
        if ask_floor:
            return GateDecision("ask", "high", "contains shell control characters; needs confirmation")
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

    def _classify_single(
        self,
        parts: list[str],
        *,
        trust_level: str = "trusted",
    ) -> GateDecision | None:
        if not parts:
            return None
        parts = self._normalize_parts(parts)
        if not parts:
            return GateDecision("ask", "medium", "command needs confirmation before running")
        executable = parts[0]
        basename = os.path.basename(executable)
        if executable in DENY_EXECUTABLES or basename in DENY_EXECUTABLES:
            return GateDecision("deny", "high", f"high-risk command is not allowed: {executable}")
        if executable == "git" or basename == "git":
            git_decision = self._gate_git(parts)
            if (
                git_decision.verdict == "allow"
                and trust_level != "trusted"
                and not self._bare_executable_is_system(executable)
            ):
                return GateDecision(
                    "ask",
                    "medium",
                    "the git executable for an untrusted workspace needs confirmation",
                )
            return git_decision
        if self._is_low_risk_test(parts):
            if trust_level != "trusted":
                return GateDecision(
                    "ask",
                    "medium",
                    "the workspace is untrusted; project commands need approval or Docker sandbox",
                )
            return GateDecision("allow", "low")
        if basename in ALLOW_EXECUTABLES and self._direct_allow_executable(executable):
            if trust_level != "trusted" and not self._bare_executable_is_system(executable):
                return GateDecision(
                    "ask",
                    "medium",
                    "PATH command in an untrusted workspace needs confirmation",
                )
            return GateDecision("allow", "low")
        return GateDecision("ask", "medium", f"command needs confirmation before running: {executable}")

    def _gate_git(self, parts: list[str]) -> GateDecision:
        subcommand = parts[1] if len(parts) > 1 else ""
        if subcommand in DENY_GIT_SUBCOMMANDS:
            return GateDecision("deny", "high", f"destructive git command is not allowed: git {subcommand}")
        if subcommand == "checkout" and "--" in parts:
            return GateDecision("deny", "high", "git checkout -- (discarding changes) is not allowed")
        if subcommand == "push" and any(flag in parts for flag in ("--force", "-f", "--force-with-lease", "--delete")):
            return GateDecision("deny", "high", "force/delete git push is not allowed")
        if subcommand == "branch" and any(flag in parts for flag in ("-D", "-d", "-M", "-m")):
            return GateDecision("deny", "high", "deleting/renaming branches is not allowed")
        if subcommand == "stash" and "drop" in parts:
            return GateDecision("deny", "high", "git stash drop is not allowed")
        if subcommand in ALLOW_GIT_SUBCOMMANDS:
            return GateDecision("allow", "low")
        return GateDecision("ask", "medium", f"git {subcommand} needs confirmation before running")

    def _is_low_risk_test(self, parts: list[str]) -> bool:
        executable = os.path.basename(parts[0])
        if executable == "pytest":
            return True
        if executable == "go" and len(parts) >= 2 and parts[1] == "test":
            return True
        if executable in {"npm", "pnpm", "yarn"} and "test" in parts[1:]:
            return True
        if executable in {"python", "python3"} and len(parts) >= 3 and parts[1] == "-m" and parts[2] in {"pytest", "unittest"}:
            return True
        return False

    def _gate_paths(
        self,
        segments: list[list[str]],
        *,
        workspace: Path | None,
        protected_paths: list[str],
    ) -> GateDecision | None:
        if workspace is None:
            return None
        root = workspace.expanduser().resolve()
        for segment in segments:
            parts = self._normalize_parts(segment)
            if not parts:
                continue
            for executable_token in dict.fromkeys((segment[0], parts[0])):
                executable_decision = self._gate_executable_path(executable_token, root)
                if executable_decision is not None:
                    return executable_decision
            executable = os.path.basename(parts[0])
            if executable in _SHELL_WRAPPERS and "-c" in parts:
                index = parts.index("-c")
                if index + 1 < len(parts):
                    nested = self.gate_bash(
                        parts[index + 1],
                        workspace=root,
                        protected_paths=protected_paths,
                    )
                    if nested.verdict == "deny":
                        return nested
            path_tokens = [*segment[1:], *parts[1:]]
            for token in dict.fromkeys(path_tokens):
                decision = self._gate_path_token(token, root, protected_paths)
                if decision is not None:
                    return decision
        return None

    def _gate_executable_path(
        self,
        token: str,
        workspace: Path,
    ) -> GateDecision | None:
        if "/" not in token and "\\" not in token:
            return None
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        resolved = candidate.resolve(strict=False)
        if self._is_system_executable_path(resolved):
            return None
        try:
            resolved.relative_to(workspace)
        except ValueError:
            return self._path_deny(
                "command attempts to execute a path outside the workspace",
            )
        return None

    def _gate_path_token(
        self,
        raw_token: str,
        workspace: Path,
        protected_paths: list[str],
    ) -> GateDecision | None:
        token = raw_token.strip()
        if not token or token == ".":
            return None
        if token.startswith("-") and "=" not in token:
            return None
        if "=" in token and token.startswith("-"):
            token = token.split("=", 1)[1]
        token = token.rstrip(",;")
        lowered = token.lower().replace("\\", "/")
        if "://" in lowered:
            return None
        if (
            lowered == ".."
            or lowered.startswith("../")
            or "/../" in lowered
            or lowered.endswith("/..")
            or lowered.startswith("~/")
            or "$home" in lowered
            or "${home}" in lowered
        ):
            return self._path_deny("command path escapes the workspace or references user home")
        if self._is_sensitive_token(lowered):
            return self._path_deny("command attempts to access a sensitive path")

        if any(marker in token for marker in ("*", "?", "[")) and not Path(token).is_absolute():
            try:
                matches = list(workspace.glob(token))[:100]
            except (OSError, ValueError):
                matches = []
            for match in matches:
                decision = self._gate_resolved_path(match, workspace, protected_paths)
                if decision is not None:
                    return decision

        path_like = "/" in token or token.startswith(".") or Path(token).is_absolute()
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        if not path_like and not candidate.exists():
            return None
        return self._gate_resolved_path(candidate, workspace, protected_paths)

    def _gate_resolved_path(
        self,
        candidate: Path,
        workspace: Path,
        protected_paths: list[str],
    ) -> GateDecision | None:
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(workspace)
        except ValueError:
            return self._path_deny("command path escapes the workspace boundary")
        relative_text = relative.as_posix()
        if is_protected_path(relative_text, protected_paths) or self._is_sensitive_token(relative_text.lower()):
            return self._path_deny("command attempts to access a protected/masked path")
        return None

    @staticmethod
    def _is_sensitive_token(value: str) -> bool:
        parts = {part for part in value.replace("\\", "/").split("/") if part}
        if any(marker in parts for marker in _SENSITIVE_PATH_MARKERS):
            return True
        return any(part.startswith(".env") for part in parts)

    @staticmethod
    def _path_deny(message: str) -> GateDecision:
        return GateDecision("deny", "high", message)

    @staticmethod
    def _direct_allow_executable(executable: str) -> bool:
        if "/" not in executable and "\\" not in executable:
            return True
        return PolicyEngine._is_system_executable_path(Path(executable).resolve(strict=False))

    @staticmethod
    def _is_system_executable_path(path: Path) -> bool:
        return any(path.parent == root for root in _SYSTEM_EXECUTABLE_ROOTS)

    @staticmethod
    def _bare_executable_is_system(executable: str) -> bool:
        if "/" in executable or "\\" in executable:
            return PolicyEngine._is_system_executable_path(Path(executable).resolve(strict=False))
        resolved = shutil.which(executable)
        return bool(resolved and PolicyEngine._is_system_executable_path(Path(resolved).resolve(strict=False)))
