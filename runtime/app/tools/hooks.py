"""Project-declared commands that run around tool events.

Two triggers, matching the two things people actually want: `post_edit` to
format a file that was just written, and `pre_bash` to gate a command — a lint
run that must pass before `git commit` is allowed through.

The rule that shapes everything here: **a hook command goes down the same
execution, policy and audit path as a command the model asked for.** A hook that
ran outside those would be a way to execute anything with none of the checks,
which is the opposite of what a project-level format-on-write is supposed to be.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from app.agent.policy import PolicyEngine
from app.project.config import HookRef
from app.tools.base import ToolContext
from app.tools.command import run_shell_command

# A hook's own output is diagnostic, not content. Enough to see which rule
# failed, not enough for a chatty formatter to crowd out the conversation.
MAX_HOOK_OUTPUT_CHARS = 4_000


@dataclass(slots=True)
class HookOutcome:
    """What running the hooks for one event produced."""

    ran: list[str]
    blocked_by: str = ""
    reason: str = ""
    output: str = ""

    @property
    def ok(self) -> bool:
        return not self.blocked_by


def matching_hooks(hooks: list[HookRef], event: str, target: str) -> list[HookRef]:
    selected = []
    for hook in hooks:
        if hook.event != event:
            continue
        if hook.match and not _matches(hook.match, target):
            continue
        selected.append(hook)
    return selected


def _matches(pattern: str, target: str) -> bool:
    if fnmatch(target, pattern):
        return True
    # `*.py` should catch `src/app.py`. fnmatch alone anchors on the whole
    # string, so a bare extension pattern would miss every nested file and the
    # hook would look broken rather than unmatched.
    return "/" not in pattern and fnmatch(Path(target).name, pattern)


def render_hook_command(template: str, values: dict[str, str]) -> str:
    """Substitute `{path}` / `{command}` into a hook's command line.

    Every value is shell-quoted. A path is attacker-influenced in the cases that
    matter — a repo can contain `a; rm -rf ~.py` — so pasting it in raw would
    turn "format the file I just wrote" into arbitrary command execution.
    """
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", shlex.quote(value))
    return rendered


async def run_hooks(
    context: ToolContext,
    event: str,
    *,
    target: str,
    values: dict[str, str] | None = None,
    policy: PolicyEngine | None = None,
) -> HookOutcome:
    hooks = matching_hooks(list(getattr(context, "hooks", []) or []), event, target)
    if not hooks:
        return HookOutcome(ran=[])
    if context.trust_level != "trusted":
        # `.aicode/config.json` arrives with the repository, so a hook is code
        # chosen by whoever wrote that repo. Running it merely because the user
        # opened the directory would make cloning a hostile project enough to
        # execute its commands. Reported rather than skipped silently, so a hook
        # that does not fire is not mistaken for one that passed.
        return HookOutcome(
            ran=[],
            blocked_by="",
            reason=(
                f"{len(hooks)} {event} hook(s) declared by this project were not run because the "
                "workspace is not trusted. Run `aicode project trust add` to enable them."
            ),
        )

    engine = policy or PolicyEngine()
    outputs: list[str] = []
    ran: list[str] = []
    for hook in hooks:
        command = render_hook_command(hook.command, values or {})
        gate = engine.gate_bash(
            command,
            workspace=context.workspace,
            protected_paths=context.protected_paths,
            trust_level=context.trust_level,
        )
        if gate.verdict == "deny":
            # The same denial the model would get. A project cannot reach past
            # policy by putting the command in a config file instead.
            return HookOutcome(
                ran=ran,
                blocked_by=hook.command,
                reason=f"hook command denied by policy: {gate.reason}",
                output="\n".join(outputs),
            )
        result = await run_shell_command(
            command,
            cwd=context.workspace,
            timeout=hook.timeout,
            stderr_to_stdout=True,
            execution=context.execution,
            backend=context.resolved_bash_backend(),
            metadata={
                "action": f"hook.{event}",
                "mode": context.mode,
                "session_id": context.session_id,
                "run_id": context.run_id,
                "tool_call_id": context.tool_call_id,
                "masked_paths": context.protected_paths,
                "trust_level": context.trust_level,
            },
        )
        ran.append(hook.command)
        text = (result.stdout or "").strip()
        if text:
            outputs.append(f"$ {hook.command}\n{text}"[:MAX_HOOK_OUTPUT_CHARS])
        if result.returncode == 0:
            continue
        detail = "timed out" if result.timed_out else f"exit={result.returncode}"
        if hook.blocking and event == "pre_bash":
            return HookOutcome(
                ran=ran,
                blocked_by=hook.command,
                reason=f"hook `{hook.command}` failed ({detail})",
                output="\n".join(outputs),
            )
        # A post_edit hook cannot block: the edit is already on disk. Its failure
        # is still surfaced, because a formatter that silently stopped working is
        # how a repo drifts out of format without anyone noticing.
        outputs.append(f"[hook `{hook.command}` failed: {detail}]")
    return HookOutcome(ran=ran, output="\n".join(outputs)[:MAX_HOOK_OUTPUT_CHARS])


def hook_event_payload(event: str, outcome: HookOutcome, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "hook.finished" if outcome.ok else "hook.blocked",
        "event": event,
        "hooks": outcome.ran,
    }
    if outcome.reason:
        payload["reason"] = outcome.reason
    payload.update(extra)
    return payload
