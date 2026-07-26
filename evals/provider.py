from __future__ import annotations

from typing import Any

from app.agent.history import estimate_tokens
from app.models.provider import (
    CompletionRequest,
    ProviderError,
    StreamEvent,
    ToolCallRequest,
    Usage,
)

from evals.contracts import EvalBudgets, ModelProfile, ScriptedTurn


class EvalBudgetExceeded(ProviderError):
    pass


class ScriptedEvalProvider:
    provider_name = "scripted"

    def __init__(self, turns: list[ScriptedTurn], profile: ModelProfile, budgets: EvalBudgets) -> None:
        self.provider_name = profile.provider
        self.turns = list(turns)
        self.profile = profile
        self.budgets = budgets
        self.calls: list[dict[str, Any]] = []
        self.total_tokens = 0
        self.total_cost = 0.0

    def is_configured(self) -> bool:
        return True

    async def stream_complete(self, request: CompletionRequest):
        call: dict[str, Any] = {
            "call_index": len(self.calls) + 1,
            "purpose": request.purpose,
            "model": request.model,
            "message_count": len(request.messages),
            "input_estimate": estimate_tokens(
                {"system": request.system, "messages": request.messages, "tools": request.tools}
            ),
            "offered_tools": [str(tool.get("name") or "") for tool in request.tools],
            "max_tokens": request.max_tokens,
            "status": "started",
        }
        self.calls.append(call)
        try:
            turn = self._next_turn(request)
            next_tokens = turn.input_tokens + turn.output_tokens
            next_cost = (
                turn.input_tokens * self.profile.input_per_1m
                + turn.output_tokens * self.profile.output_per_1m
            ) / 1_000_000
            if self.total_tokens + next_tokens > self.budgets.max_tokens:
                raise EvalBudgetExceeded("eval token budget exceeded")
            if self.total_cost + next_cost > self.budgets.max_cost:
                raise EvalBudgetExceeded("eval cost budget exceeded")
            self.total_tokens += next_tokens
            self.total_cost += next_cost
            call["response"] = {
                "text": turn.text,
                "tool_calls": [tool.model_dump() for tool in turn.tool_calls],
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
            }
            if turn.text:
                yield StreamEvent(type="text_delta", text=turn.text)
            for tool in turn.tool_calls:
                yield StreamEvent(
                    type="tool_call",
                    tool_call=ToolCallRequest(id=tool.id, name=tool.name, arguments=tool.arguments),
                )
            yield StreamEvent(
                type="done",
                usage=Usage(input_tokens=turn.input_tokens, output_tokens=turn.output_tokens),
                model=self.profile.model,
            )
            call["status"] = "completed"
        except Exception as exc:
            call["status"] = "failed"
            call["error_type"] = exc.__class__.__name__
            raise

    def _next_turn(self, request: CompletionRequest) -> ScriptedTurn:
        if len(self.calls) > self.budgets.max_model_calls:
            raise EvalBudgetExceeded("eval model call budget exceeded")
        if not self.turns:
            raise ProviderError("scripted eval provider has no remaining turn")
        turn = self.turns.pop(0)
        if turn.purpose is not None and turn.purpose != request.purpose:
            raise ProviderError(
                f"scripted turn expected purpose={turn.purpose}, received purpose={request.purpose}"
            )
        return turn
