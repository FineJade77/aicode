from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.provider import CompletionResult


@dataclass(slots=True)
class TurnBudget:
    max_steps: int = 40
    max_tokens_per_call: int = 8192


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def user_note(text: str) -> dict[str, Any]:
    return {"role": "user", "content": f"[系统提示] {text}"}


def assistant_message(result: CompletionResult) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": result.text}
    if result.tool_calls:
        message["tool_calls"] = [call.to_dict() for call in result.tool_calls]
    return message


def tool_message(tool_call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}
