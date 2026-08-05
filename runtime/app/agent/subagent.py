"""A focused exploration run with its own context, reporting back a summary.

The point is context, not capability: reading twenty files to answer one
question fills the main history with output that only mattered while the answer
was being found. A subagent does that reading against its own message list and
hands back the conclusion.

Everything else is deliberately *not* separate. The roadmap's own condition for
this feature is that a subagent reuse budget, policy, execution and trace,
because a second path that skips them is exactly what the rest of the
architecture exists to prevent. So:

- **Read-only tools only.** Not a limitation to be lifted later: an approval
  prompt raised inside a run the user cannot see, for an edit that never appears
  in the main transcript, is an approval in name only. Read-only tools need no
  approval, so the question does not arise.
- **Budget is carved out of the parent's, never added to it.** Otherwise
  spawning subagents is how a turn escapes its own spend limit.
- **Depth one.** A subagent cannot spawn another; budget accounting over a tree
  has failure modes worth avoiding for a feature whose value is still unproven.
- **Same policy, same execution service, same audit sink**, because they are the
  parent runtime's, passed through untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.agent.history import estimate_tokens
from app.agent.turn import TurnBudget, TurnLedger, assistant_message, tool_message, user_message
from app.models.provider import CompletionResult, ToolCallRequest

# The read-only surface. Named here rather than derived from `spec.read_only` so
# adding a read-only tool does not silently widen what a subagent may do.
SUBAGENT_TOOLS = ("read_file", "search", "glob", "list_files", "related_files")

# A subagent gets a slice of the parent's remaining budget, not a fresh one.
SUBAGENT_BUDGET_SHARE = 0.25
MAX_SUBAGENT_STEPS = 12
MAX_REPORT_CHARS = 4_000

SUBAGENT_SYSTEM = """You are a focused research subagent for a coding agent.

You have read-only tools. You cannot edit files or run commands, and nothing you
do is visible to the user, so do not address them.

Answer the question you were given by reading the workspace, then reply with a
compact report: the answer first, then the specific files and line ranges that
support it. Cite paths so the caller can go straight there. Do not speculate
beyond what you read; if the workspace does not answer the question, say so."""


@dataclass(slots=True)
class SubagentOutcome:
    report: str
    model_calls: int
    total_tokens: int
    total_cost: float
    tool_calls: int
    stop_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_calls": self.model_calls,
            "total_tokens": self.total_tokens,
            "total_cost": round(self.total_cost, 8),
            "tool_calls": self.tool_calls,
            "stop_reason": self.stop_reason,
            "report_chars": len(self.report),
        }


def subagent_budget(parent: TurnBudget, spent: TurnLedger) -> TurnBudget:
    """Carve a slice out of what the parent has left.

    Taken from the *remaining* budget rather than the original, so a turn that
    has already spent most of its allowance cannot hand a full one to a child.
    """
    remaining_tokens = max(0, parent.max_total_tokens - spent.total_tokens) if parent.max_total_tokens > 0 else 0
    remaining_cost = max(0.0, parent.max_total_cost - spent.total_cost) if parent.max_total_cost > 0 else 0.0
    return replace(
        parent,
        max_steps=min(MAX_SUBAGENT_STEPS, parent.max_steps),
        max_total_tokens=int(remaining_tokens * SUBAGENT_BUDGET_SHARE),
        max_total_cost=remaining_cost * SUBAGENT_BUDGET_SHARE,
        # The child reports; it never verifies or re-plans, so the turn-shaping
        # gates that exist for the main loop do not apply to it.
        max_verify_rounds=0,
        max_repeated_actions=0,
    )


async def run_subagent(
    *,
    runtime: Any,
    context: Any,
    question: str,
    budget: TurnBudget,
) -> SubagentOutcome:
    """Run the exploration loop and return its report.

    A plain loop rather than a reuse of `run_turn`: the main loop's job is to
    drive a session — persisting messages, emitting events, gating approvals,
    tracking verification. A subagent has no session and no user, and pretending
    otherwise would mean threading "is this the real one?" through all of it.
    What it does share is the pieces that enforce anything: the tool runtime,
    the policy engine, the execution service and the trace sink, all reached
    through the same `runtime` and `context` the parent uses.
    """
    assert runtime.model_runtime is not None and runtime.tools is not None
    schemas = [
        schema
        for schema in _schemas(runtime, context)
        if schema.get("name") in SUBAGENT_TOOLS
    ]
    messages: list[dict[str, Any]] = [user_message(question)]
    ledger = TurnLedger()
    tool_calls = 0
    stop_reason = ""
    report = ""

    for _step in range(max(1, budget.max_steps)):
        result: CompletionResult = await runtime.model_runtime.stream_complete(
            purpose="summarizer",
            system=SUBAGENT_SYSTEM,
            messages=messages,
            tools=schemas,
            max_tokens=budget.max_tokens_per_call,
        )
        ledger.add(result)
        messages.append(assistant_message(result))
        exceeded = ledger.exceeded(budget)
        if exceeded is not None:
            stop_reason = exceeded
            report = result.text.strip()
            break
        if not result.tool_calls:
            report = result.text.strip()
            break
        for call in result.tool_calls:
            tool_calls += 1
            messages.append(tool_message(call.id, await _run_tool(runtime, context, call)))
    else:
        stop_reason = "steps"

    if not report:
        report = "The subagent stopped before reaching a conclusion."
    if len(report) > MAX_REPORT_CHARS:
        # A report longer than the reading it replaces defeats the purpose.
        report = report[:MAX_REPORT_CHARS] + "\n[report truncated]"
    return SubagentOutcome(
        report=report,
        model_calls=ledger.model_calls,
        total_tokens=ledger.total_tokens,
        total_cost=ledger.total_cost,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
    )


async def _run_tool(runtime: Any, context: Any, call: ToolCallRequest) -> str:
    """Execute one read-only call through the parent's own tool runtime.

    Refusing an unlisted name here is the second half of the allowlist: the
    schemas were filtered, but a model can still emit a name it was not offered,
    and honouring it would let a subagent reach a tool the caller never granted.
    """
    if call.name not in SUBAGENT_TOOLS:
        return f"[refused] {call.name} is not available to a subagent"
    error = runtime.tools.validate_arguments(call.name, call.arguments)
    if error is not None:
        return f"[invalid arguments] {error}"
    try:
        result = await runtime.tools.run(call.name, call.arguments, context)
    except Exception as exc:  # noqa: BLE001 - a failing read must not end the parent turn
        return f"[tool error] {exc}"
    text = result.text if result.success else f"[tool error] {result.error}"
    return text or "[no output]"


def _schemas(runtime: Any, context: Any) -> list[dict[str, Any]]:
    try:
        return runtime.tools.schemas_for_mode("default", workspace=str(context.workspace))
    except TypeError:
        return runtime.tools.schemas_for_mode("default")


def report_tokens(report: str) -> int:
    return estimate_tokens(report)


def workspace_of(context: Any) -> Path:
    return Path(context.workspace)
