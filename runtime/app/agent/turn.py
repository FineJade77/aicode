from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.provider import CompletionResult
from app.security import redact_known_environment_secrets


@dataclass(slots=True)
class TurnBudget:
    max_steps: int = 40
    max_tokens_per_call: int = 8192
    # Cumulative caps for one turn. `max_steps` alone does not bound spend: a
    # model looping over tool calls can burn a large amount inside 40 steps, and
    # every step re-sends the whole history. 0 disables a cap.
    max_total_tokens: int = 1_000_000
    max_total_cost: float = 5.0
    # How many times the loop pushes an unverified model back before winding down
    # and reporting. 0 disables the check, restoring "finish whenever the model
    # says it is done".
    max_verify_rounds: int = 3
    # Consecutive identical actions (or identical failures) before the turn stops
    # and reports. Catches the stuck case that `max_steps` cannot distinguish from
    # a genuinely long task. 0 or 1 disables the check.
    max_repeated_actions: int = 5


@dataclass(slots=True)
class TurnLedger:
    """Cumulative usage for a single turn, owned by the Agent Loop.

    Usage was previously only reported to the event stream and trace; this makes
    it participate in control flow so a runaway turn stops instead of spending
    without bound.
    """

    total_tokens: int = 0
    total_cost: float = 0.0
    model_calls: int = 0

    def add(self, result: CompletionResult) -> None:
        self.total_tokens += max(0, result.input_tokens) + max(0, result.output_tokens)
        self.total_cost += max(0.0, float(result.estimated_cost or 0.0))
        self.model_calls += 1

    def exceeded(self, budget: TurnBudget) -> str | None:
        """Return the exhausted budget dimension, or None while within limits."""
        if budget.max_total_tokens > 0 and self.total_tokens >= budget.max_total_tokens:
            return "tokens"
        if budget.max_total_cost > 0 and self.total_cost >= budget.max_total_cost:
            return "cost"
        return None

    def limit_for(self, budget: TurnBudget, reason: str) -> float:
        return {"tokens": float(budget.max_total_tokens), "cost": budget.max_total_cost}.get(reason, 0.0)


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": redact_known_environment_secrets(text)}


def user_note(text: str) -> dict[str, Any]:
    return {"role": "user", "content": f"[system note] {text}"}


def assistant_message(result: CompletionResult) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": redact_known_environment_secrets(result.text),
    }
    if result.tool_calls:
        message["tool_calls"] = redact_known_environment_secrets(
            [call.to_dict() for call in result.tool_calls]
        )
    return message


MESSAGE_META_KEY = "aicode_meta"


def tool_message(tool_call_id: str, content: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": redact_known_environment_secrets(content),
    }
    if meta:
        # Runtime-private provenance, persisted with the message but stripped
        # before the history is handed to a provider. Kept on the message rather
        # than in a side table so it cannot drift out of sync with the message it
        # describes, and so it survives a daemon restart with the session.
        message[MESSAGE_META_KEY] = meta
    return message
