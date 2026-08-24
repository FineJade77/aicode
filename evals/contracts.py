from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ToolCallSpec(BaseModel):
    id: str
    name: str
    arguments: dict = Field(default_factory=dict)


class ScriptedTurn(BaseModel):
    purpose: Literal["main", "reviewer", "summarizer"] | None = None
    text: str = ""
    tool_calls: list[ToolCallSpec] = Field(default_factory=list)
    input_tokens: int = Field(default=10, ge=0)
    output_tokens: int = Field(default=5, ge=0)


class ModelProfile(BaseModel):
    provider: Literal["scripted", "anthropic", "openai_compatible"]
    model: str
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    input_per_1m: float = Field(default=0.0, ge=0)
    output_per_1m: float = Field(default=0.0, ge=0)


class EvalBudgets(BaseModel):
    max_model_calls: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_cost: float = Field(ge=0)
    wall_time_seconds: float = Field(gt=0)


class HistorySeed(BaseModel):
    message_count: int = Field(default=0, ge=0, le=1_000)
    chars_per_message: int = Field(default=0, ge=0, le=100_000)
    # Size the seed as a fraction of the model's context window instead of a
    # fixed character count. "Enough history to force a compaction" only means
    # something relative to the window: a seed sized for an 8k window compacts
    # there and silently does nothing at 200k, so the task would assert a
    # compaction that can never happen. Set above `context.compact_threshold`
    # (0.8 by default). 0 keeps the absolute `chars_per_message`.
    target_context_ratio: float = Field(default=0.0, ge=0.0, le=4.0)


class FileAssertion(BaseModel):
    path: str
    exists: bool = True
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)


class MutationCheck(BaseModel):
    """A deliberate defect the task's tests must catch.

    Grading "the Agent added tests" on the tests passing is vacuous — an empty
    test file passes. So the grader re-runs the suite against a broken copy of
    the implementation and requires it to fail.

    The mutation lives in the task JSON rather than in the fixture on purpose:
    anything placed in the workspace is readable by the Agent, and a discoverable
    answer key turns "write a test for the empty-input boundary" into "read which
    line we broke".
    """

    path: str
    old_text: str = Field(min_length=1)
    new_text: str
    # Which of `test_commands` must fail against the mutant. Defaults to the
    # first, which is the suite command in every task written so far.
    command_index: int = Field(default=0, ge=0)


class EvalChecks(BaseModel):
    test_commands: list[list[str]] = Field(default_factory=list)
    mutations: list[MutationCheck] = Field(default_factory=list)
    files: list[FileAssertion] = Field(default_factory=list)
    allowed_changed_paths: list[str] = Field(default_factory=list)
    forbidden_changed_paths: list[str] = Field(default_factory=list)
    required_events: list[str] = Field(default_factory=list)
    forbidden_events: list[str] = Field(default_factory=list)
    required_audit_events: list[str] = Field(default_factory=list)
    final_contains: list[str] = Field(default_factory=list)
    minimum_compactions: int = Field(default=0, ge=0)
    maximum_agent_executions: int | None = Field(default=None, ge=0)
    # Execution backends that must never appear in the audit trail for this task.
    # Asserting "no host execution" is stable whether or not the grading machine
    # has Docker: with Docker the command is sandboxed, without it the command is
    # refused, and only a regression to host fallback produces a host execution.
    forbidden_execution_backends: list[str] = Field(default_factory=list)
    expected_approvals: dict[Literal["edit", "tool"], Literal["accept", "reject"]] = Field(default_factory=dict)
    # The Agent must ask before it edits.
    #
    # For a request the repository cannot disambiguate, "produced a change" is
    # not the property worth grading — either branch compiles and passes its own
    # reading. What separates a good run from a lucky one is whether the Agent
    # recognised that it could not know, so the assertion is on the order of
    # `question.asked` and the first `edit.applied`, not on which branch it took.
    expects_question_before_edit: bool = False


class EvalTask(BaseModel):
    contract_version: Literal["1.0"]
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    suite: str
    description: str
    fixture: str
    user_request: str
    mode: Literal["default", "review", "diff", "test", "explain", "commit_message"] = "default"
    tags: list[str] = Field(default_factory=list)
    trust: Literal["trusted", "untrusted"] = "untrusted"
    approval_policy: dict[Literal["edit", "tool"], Literal["accept", "reject"]] = Field(
        default_factory=lambda: {"edit": "accept", "tool": "reject"}
    )
    profile: ModelProfile
    budgets: EvalBudgets
    history_seed: HistorySeed = Field(default_factory=HistorySeed)
    # The reply the harness gives to any `ask_user`. Fixed rather than modelled,
    # so the answer is part of the task rather than a second thing to evaluate.
    question_answer: str = ""
    # Shell backend for the task's Agent commands. None leaves it to the
    # resolved default, which is what most tasks want; a task that is *about*
    # the sandbox names it, so the assertion does not silently become vacuous
    # when the default moves.
    bash_backend: Literal["auto", "host", "docker", "os"] | None = None
    # `scripted` replays `model_script` and proves the Agent Loop is implemented
    # correctly. `live` calls a real model and measures whether the Agent can
    # finish the task at all — a different question, so it is a mode rather than
    # a replacement: the scripted suite stays in CI for its zero-cost,
    # zero-jitter regression value.
    provider_mode: Literal["scripted", "live"] = "scripted"
    # Overrides the model actually called in live mode. `profile.model` still
    # drives context window and pricing, so a model swap that forgets this stays
    # visible rather than silently billing against the wrong price table.
    live_model: str | None = None
    model_script: list[ScriptedTurn] = Field(default_factory=list)
    checks: EvalChecks
    trace_redactions: list[str] = Field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return self.provider_mode == "live"

    @model_validator(mode="after")
    def validate_script_budget(self) -> EvalTask:
        if self.provider_mode == "scripted":
            if not self.model_script:
                raise ValueError("scripted tasks require a non-empty model_script")
            if self.profile.provider != "scripted":
                raise ValueError("scripted tasks require profile.provider 'scripted'")
            if len(self.model_script) > self.budgets.max_model_calls:
                raise ValueError("model_script exceeds budgets.max_model_calls")
            return self
        # A live task carrying a script would silently ignore it, which reads as
        # a working assertion that never runs.
        if self.model_script:
            raise ValueError("live tasks must not define a model_script")
        if self.profile.provider == "scripted":
            raise ValueError("live tasks require a real profile.provider")
        return self


def load_task(path: Path) -> EvalTask:
    return EvalTask.model_validate_json(path.read_text(encoding="utf-8"))


def task_digest(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
