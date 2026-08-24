from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_MAX_VERIFY_ROUNDS = 3
MAX_SUMMARY_LINES = 6
MAX_SUMMARY_CHARS = 600

# Lines worth surfacing from a failed verification run. Ordered by how specific
# they are, so a pytest `FAILED test::name` wins over a generic `error:`.
FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^FAILED\s+\S+", re.MULTILINE),           # pytest
    re.compile(r"^ERROR\s+\S+", re.MULTILINE),            # pytest collection errors
    re.compile(r"^\s*--- FAIL: \S+", re.MULTILINE),       # go test
    re.compile(r"^\s*✕\s+\S.*", re.MULTILINE),            # jest / vitest
    re.compile(r"^\S+:\d+:\d+:\s+\S.*", re.MULTILINE),    # compiler / linter positions
    re.compile(r"^\S*(?:Error|error):\s+\S.*", re.MULTILINE),
    re.compile(r"^\s*assert\b.*", re.MULTILINE),
)


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    command: str
    passed: bool
    exit_code: int
    summary: str = ""

    def to_event(self, round_number: int, limit: int) -> dict[str, object]:
        return {
            "type": "verify.attempt",
            "round": round_number,
            "limit": limit,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "summary": self.summary,
        }


@dataclass(slots=True)
class VerifyTracker:
    """Bounds the repair loop that follows an applied edit.

    The prompt has always told the model to "stop and report after 3 consecutive
    failed attempts", but nothing enforced it: the verification nudge was a
    one-shot flag, so a model could claim it was done, get pushed back once, then
    claim done again and exit. Verification was effectively optional, and a model
    that kept trying was bounded only by the step budget.

    What this guarantees is narrow and worth stating plainly: the loop pushes an
    unverified model back a bounded number of times and then produces a summary,
    instead of exhausting steps silently. It does not adjudicate whether the model
    ran the *right* command — the Runtime cannot know that for an arbitrary
    project.
    """

    limit: int = DEFAULT_MAX_VERIFY_ROUNDS
    rounds: int = 0
    verified: bool = False
    failures: list[VerifyOutcome] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def note_edit_applied(self) -> None:
        """A new edit invalidates any earlier passing run."""
        self.verified = False

    def record(self, outcome: VerifyOutcome) -> None:
        if outcome.passed:
            self.verified = True
            self.failures.clear()
            return
        self.verified = False
        self.failures.append(outcome)

    def should_request_verification(self, applied_edits: int) -> bool:
        return self.enabled and applied_edits > 0 and not self.verified and self.rounds < self.limit

    def exhausted(self, applied_edits: int) -> bool:
        return self.enabled and applied_edits > 0 and not self.verified and self.rounds >= self.limit

    def begin_round(self) -> int:
        self.rounds += 1
        return self.rounds

    def failure_digest(self) -> str:
        if not self.failures:
            return ""
        latest = self.failures[-1]
        detail = f" ({latest.summary})" if latest.summary else ""
        return f"exit={latest.exit_code}{detail}"


def summarize_failure(output: str) -> str:
    """Pull the few lines that identify a failure out of a command's output.

    Used for the event and the wind-down note, not to replace the tool output the
    model already receives: the point is that the user and the final summary get
    something specific instead of a wall of stderr.
    """
    text = (output or "").strip()
    if not text:
        return ""
    picked: list[str] = []
    seen: set[str] = set()
    for pattern in FAILURE_PATTERNS:
        for match in pattern.findall(text):
            line = " ".join(str(match).split())
            if line and line not in seen:
                seen.add(line)
                picked.append(line)
            if len(picked) >= MAX_SUMMARY_LINES:
                break
        if len(picked) >= MAX_SUMMARY_LINES:
            break
    if not picked:
        # Nothing recognisable: the last non-empty line is usually the verdict.
        tail = [line.strip() for line in text.splitlines() if line.strip()]
        picked = tail[-1:]
    return "; ".join(picked)[:MAX_SUMMARY_CHARS]
