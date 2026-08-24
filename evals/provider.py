from __future__ import annotations

from typing import Any

from app.agent.history import estimate_tokens
from app.models.provider import (
    CompletionRequest,
    ProviderError,
    StreamEvent,
    StreamingModelProvider,
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


class LiveEvalProvider:
    """Calls a real model while accounting exactly like the scripted provider.

    The scripted provider proves the Agent Loop replays a script correctly; this
    one measures whether the Agent finishes a real task. To keep that difference
    from leaking into the harness, it exposes the same `calls` / `total_tokens` /
    `total_cost` surface, so `run_metrics`, the trace writer and the budget
    checks stay free of a scripted-vs-live branch.

    Budgets are enforced here rather than only reported: a live suite that
    overruns its token budget spends real money, so the ceiling has to stop the
    run instead of describing it afterwards. Exhaustion raises the same
    `EvalBudgetExceeded` the scripted path uses, which is what makes "budget
    exhausted" a distinguishable failure category rather than a generic error.
    """

    provider_name = "live"

    def __init__(
        self,
        inner: StreamingModelProvider,
        profile: ModelProfile,
        budgets: EvalBudgets,
        *,
        model: str | None = None,
    ) -> None:
        self.inner = inner
        self.provider_name = getattr(inner, "provider_name", profile.provider)
        self.profile = profile
        self.budgets = budgets
        self.model = model or profile.model
        self.calls: list[dict[str, Any]] = []
        self.total_tokens = 0
        self.total_cost = 0.0
        # Scripted tasks assert the script was fully consumed. Live tasks have no
        # script, and the grader reads this to keep that check comparable.
        self.turns: list[ScriptedTurn] = []

    def is_configured(self) -> bool:
        is_configured = getattr(self.inner, "is_configured", None)
        return bool(is_configured()) if callable(is_configured) else True

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
        if len(self.calls) > self.budgets.max_model_calls:
            call["status"] = "failed"
            call["error_type"] = "EvalBudgetExceeded"
            raise EvalBudgetExceeded("eval model call budget exceeded")
        # The live model is chosen here rather than by the caller so a task's
        # `live_model` cannot be bypassed by a purpose-specific route.
        outbound = replace_model(request, self.model)
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        usage = Usage()
        model_name = outbound.model
        try:
            async for event in self.inner.stream_complete(outbound):
                if event.type == "tool_call" and event.tool_call is not None:
                    tool_calls.append(event.tool_call.to_dict())
                elif event.type == "text_delta":
                    text_parts.append(event.text)
                elif event.type == "done":
                    usage = event.usage or usage
                    model_name = event.model or model_name
                yield event
            spent = usage.input_tokens + usage.output_tokens
            cost = (
                usage.input_tokens * self.profile.input_per_1m
                + usage.output_tokens * self.profile.output_per_1m
            ) / 1_000_000
            self.total_tokens += spent
            self.total_cost += cost
            call["response"] = {
                "text": "".join(text_parts),
                "tool_calls": tool_calls,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }
            call["status"] = "completed"
            # Checked after the fact, not before: real token counts are only
            # known once the response lands. Stopping here still prevents the
            # *next* call, which is what bounds the spend.
            if self.total_tokens > self.budgets.max_tokens:
                raise EvalBudgetExceeded("eval token budget exceeded")
            if self.total_cost > self.budgets.max_cost:
                raise EvalBudgetExceeded("eval cost budget exceeded")
        except Exception as exc:
            call["status"] = "failed"
            call["error_type"] = exc.__class__.__name__
            raise

    async def aclose(self) -> None:
        aclose = getattr(self.inner, "aclose", None)
        if callable(aclose):
            await aclose()


def replace_model(request: CompletionRequest, model: str) -> CompletionRequest:
    if request.model == model:
        return request
    return CompletionRequest(
        purpose=request.purpose,
        system=request.system,
        messages=request.messages,
        tools=request.tools,
        model=model,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )
