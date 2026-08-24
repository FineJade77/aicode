"""No-progress detection for the agent loop.

`max_steps` is the same number for "change one line" and "refactor across six
files", so tuning it trades one failure mode for the other. What actually
distinguishes a long task from a stuck one is not step count but repetition: a
model calling the same tool with the same arguments, or collecting the same
failure over and over, is not making progress and will not start to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_REPEATED_ACTIONS = 5
MAX_SIGNATURE_CHARS = 2_000

REPEATED_ACTION = "repeated_action"
REPEATED_FAILURE = "repeated_failure"


def action_signature(tool: str, arguments: Any) -> str:
    """Identify "the same call again" stably across turns.

    Keys are sorted so argument ordering cannot disguise a repeat, and any value
    that will not serialise falls back to its repr rather than raising — a
    signature that cannot be computed should degrade to "not a repeat", never to
    an exception in the loop.
    """
    try:
        rendered = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        rendered = repr(arguments)
    return f"{tool}\x00{rendered}"[:MAX_SIGNATURE_CHARS]


def failure_signature(tool: str, output: str) -> str:
    return f"{tool}\x00{' '.join((output or '').split())}"[:MAX_SIGNATURE_CHARS]


@dataclass(slots=True)
class Streak:
    signature: str = ""
    count: int = 0

    def observe(self, signature: str) -> int:
        if signature and signature == self.signature:
            self.count += 1
        else:
            self.signature = signature
            self.count = 1 if signature else 0
        return self.count

    def reset(self) -> None:
        self.signature = ""
        self.count = 0


@dataclass(slots=True)
class StallTracker:
    """Counts consecutive identical actions and consecutive identical failures.

    Two streaks rather than one because they catch different shapes of stuck. The
    action streak catches a model re-issuing a call verbatim. The failure streak
    catches a model varying its arguments slightly while collecting the identical
    error each time — different calls, same wall.
    """

    limit: int = DEFAULT_MAX_REPEATED_ACTIONS
    actions: Streak = field(default_factory=Streak)
    failures: Streak = field(default_factory=Streak)
    warned: bool = False
    tripped_reason: str | None = None
    tripped_count: int = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 1

    @property
    def warn_at(self) -> int:
        # Warn with enough headroom left for the model to change course, but never
        # on the first repeat: two identical calls in a row is often legitimate.
        return max(2, self.limit - 2)

    def observe(self, tool: str, arguments: Any, *, ok: bool, output: str) -> None:
        if not self.enabled:
            return
        action_count = self.actions.observe(action_signature(tool, arguments))
        if ok:
            self.failures.reset()
            failure_count = 0
        else:
            failure_count = self.failures.observe(failure_signature(tool, output))
        if action_count < self.warn_at and failure_count < self.warn_at:
            # Broke out of the streak: a later one deserves its own warning.
            self.warned = False
        for reason, count in ((REPEATED_FAILURE, failure_count), (REPEATED_ACTION, action_count)):
            if count >= self.limit and self.tripped_reason is None:
                self.tripped_reason = reason
                self.tripped_count = count

    def should_stop(self) -> bool:
        return self.enabled and self.tripped_reason is not None

    def pending_warning(self) -> tuple[str, int] | None:
        """The streak worth warning about, if this episode has not been warned yet.

        Warned once per episode rather than once per streak kind. Re-running one
        failing command trips both streaks at once, and reporting the same stuck
        situation twice tells the user nothing new. `repeated_failure` is
        preferred when both apply because it says more: the model is varying
        something that is not the cause.
        """
        if not self.enabled or self.tripped_reason is not None or self.warned:
            return None
        for reason, streak in ((REPEATED_FAILURE, self.failures), (REPEATED_ACTION, self.actions)):
            if streak.count >= self.warn_at:
                self.warned = True
                return reason, streak.count
        return None
