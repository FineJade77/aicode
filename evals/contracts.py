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
    provider: Literal["scripted"]
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


class FileAssertion(BaseModel):
    path: str
    exists: bool = True
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)


class EvalChecks(BaseModel):
    test_commands: list[list[str]] = Field(default_factory=list)
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
    model_script: list[ScriptedTurn]
    checks: EvalChecks
    trace_redactions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_script_budget(self) -> EvalTask:
        if len(self.model_script) > self.budgets.max_model_calls:
            raise ValueError("model_script exceeds budgets.max_model_calls")
        return self


def load_task(path: Path) -> EvalTask:
    return EvalTask.model_validate_json(path.read_text(encoding="utf-8"))


def task_digest(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
