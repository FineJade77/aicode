"""FakeProvider-driven end-to-end smoke test: edit, approve, verify, finalize."""
import asyncio

import pytest

from app.agent.loop import run_turn_safely
from app.agent.policy import PolicyEngine
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.config import Settings
from app.models.router import ModelRouter
from app.sessions.approvals import SessionApprovalBroker
from app.sessions.store import SessionStore
from app.system import SystemClock
from app.tools.runtime import DefaultToolRuntime
from app.tools.workspace import LocalWorkspaceRuntime
from tests.fakes import FakeProvider, text_turn, tool_turn


class Request:
    def __init__(self, workspace, message="Fix add", mode="default"):
        self.workspace = str(workspace)
        self.message = message
        self.mode = mode


@pytest.mark.asyncio
async def test_full_fix_flow(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    turns = [
        tool_turn("read_file", {"path": "calc.py"}, call_id="tc_1", text="Read the file first"),
        tool_turn("edit_file", {"path": "calc.py", "old_text": "return a - b", "new_text": "return a + b"}, call_id="tc_2"),
        tool_turn("bash", {"command": "cat calc.py"}, call_id="tc_3", text="Verify the change"),
        text_turn("Fixed the add function"),
    ]
    fake = FakeProvider(turns)
    router = ModelRouter(primary=fake, settings=Settings())
    audit = AuditLogger(path=tmp_path / "audit.jsonl")
    runtime = AgentRuntime(
        model_runtime=router,
        trace=audit,
        policy=PolicyEngine(),
        tools=DefaultToolRuntime(),
        workspace=LocalWorkspaceRuntime(),
        clock=SystemClock(),
        approvals=SessionApprovalBroker(),
    )
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))

    async def approve_all_pending():
        """Background task that approves all pending edits."""
        try:
            while True:
                await asyncio.sleep(0.01)
                for approval in list(session.approvals.values()):
                    if approval.accepted is None:
                        session.resolve_approval(approval.approval_id, accepted=True)
        except asyncio.CancelledError:
            pass

    approver = asyncio.create_task(approve_all_pending())
    try:
        await run_turn_safely(session, Request(tmp_path), runtime)
    finally:
        approver.cancel()

    # Verify that the file changed.
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"

    # Verify that all key events were emitted.
    events = session.events.events_after(0)
    types = [e["type"] for e in events]
    for expected in ["tool.started", "approval.requested", "edit.applied", "usage.recorded", "final"]:
        assert expected in types, f"missing event {expected}"

    # The model ran a command after editing and it passed, so the loop lets it
    # finish. The old implementation injected its verification note regardless of
    # whether verification had actually happened, costing an extra model call on
    # every successful edit turn.
    assert "verify.attempt" in types
    finals = [e for e in events if e["type"] == "final"]
    assert finals and finals[-1]["summary"] == "Fixed the add function"

    # Multi-turn memory: the next message should include the prior turn.
    fake.turns.append(text_turn("Continue from the prior turn"))
    await run_turn_safely(session, Request(tmp_path, message="Continue"), runtime)

    # Verify that the prior message appears in the second model call.
    last_call = fake.calls[-1]
    assert any("Fix add" in str(m.get("content")) for m in last_call.messages if m.get("role") == "user")
