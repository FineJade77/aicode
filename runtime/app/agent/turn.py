from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.provider import CompletionResult
from app.security.secrets import redact_known_environment_secrets


@dataclass(slots=True)
class TurnBudget:
    max_steps: int = 40
    max_tokens_per_call: int = 8192


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


def tool_message(tool_call_id: str, content: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": redact_known_environment_secrets(content),
    }
