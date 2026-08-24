from __future__ import annotations

from app.agent.session import ApprovalDecision
from app.tools.base import ToolContext, ToolResult

MAX_OPTIONS = 8
MAX_OPTION_CHARS = 200
MAX_QUESTION_CHARS = 1_000

TIMED_OUT_TEXT = (
    "No answer was given in time. This is not a refusal — nobody decided. "
    "Continue with the most reasonable assumption and state clearly which assumption you made, "
    "or stop and report what you need to know. Do not ask the same question again."
)
DECLINED_TEXT = (
    "The user declined to answer. Continue with the most reasonable assumption and state it, "
    "or stop and report. Do not ask again."
)
MISSING_TEXT = "The question could not be delivered, so it was not answered."


class AskUserTool:
    """Ask the user a question in the middle of a turn.

    Without this the model has two exits — keep guessing, or stop — and approvals
    cannot close the gap: an approval answers "may I do this?", not "which of
    these do you want?". When the requirement is genuinely ambiguous (which test
    framework, whether to keep an API backward compatible, whether a new
    dependency is acceptable) guessing wrong costs the whole turn, where asking
    costs one round trip.
    """

    def __init__(self, spec) -> None:
        self.spec = spec

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        question = str(args.get("question") or "").strip()[:MAX_QUESTION_CHARS]
        if not question:
            return ToolResult(success=False, error="question must not be empty")

        options = _clean_options(args.get("options"))
        broker = context.approvals
        if broker is None or context.session is None:
            # No way to reach a user. Fail loudly rather than returning a made-up
            # answer, which would be indistinguishable from a real one.
            return ToolResult(
                success=False,
                error="no interactive user is attached to this session, so the question cannot be asked",
            )

        payload: dict = {"question": question}
        if options:
            payload["options"] = options
        decision, answer = await broker.ask(context.session, payload=payload)

        if decision is ApprovalDecision.ACCEPTED and answer.strip():
            return ToolResult(
                success=True,
                text=f"The user answered: {answer.strip()}",
                data={"status": "answered", "answer": answer.strip()},
            )
        if decision is ApprovalDecision.ACCEPTED:
            # Accepted with nothing in it is an empty answer, not an answer.
            return ToolResult(success=True, text=DECLINED_TEXT, data={"status": "empty"})
        if decision is ApprovalDecision.TIMED_OUT:
            return ToolResult(success=True, text=TIMED_OUT_TEXT, data={"status": "timed_out"})
        if decision is ApprovalDecision.MISSING:
            return ToolResult(success=True, text=MISSING_TEXT, data={"status": "missing"})
        return ToolResult(success=True, text=DECLINED_TEXT, data={"status": "declined"})


def _clean_options(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    options: list[str] = []
    for entry in raw:
        if isinstance(entry, dict | list):
            continue
        option = " ".join(str(entry).split())[:MAX_OPTION_CHARS]
        if option and option not in options:
            options.append(option)
        if len(options) >= MAX_OPTIONS:
            break
    return options
