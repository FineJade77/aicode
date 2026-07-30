import asyncio

import pytest

from app.agent.session import ApprovalDecision
from app.sessions.store import Session


@pytest.mark.asyncio
async def test_session_approval_can_be_resolved() -> None:
    session = Session(session_id="sess_test", workspace="/tmp/workspace")
    approval = session.create_approval("patch", {"path": "README.md"})

    async def resolve_later() -> None:
        await asyncio.sleep(0.01)
        assert session.resolve_approval(approval.approval_id, accepted=True)

    asyncio.create_task(resolve_later())

    assert await session.wait_for_approval(approval.approval_id, timeout_seconds=1) is ApprovalDecision.ACCEPTED
    assert approval.resolution == "accepted"


@pytest.mark.asyncio
async def test_session_approval_rejects_duplicate_resolution() -> None:
    session = Session(session_id="sess_test", workspace="/tmp/workspace")
    approval = session.create_approval("patch", {"path": "README.md"})

    assert session.resolve_approval(approval.approval_id, accepted=False)
    assert not session.resolve_approval(approval.approval_id, accepted=True)
    assert await session.wait_for_approval(approval.approval_id, timeout_seconds=1) is ApprovalDecision.REJECTED
    assert approval.resolution == "rejected"


@pytest.mark.asyncio
async def test_session_approval_timeout_is_distinct_from_rejection() -> None:
    """A timeout must not be reported as a refusal.

    Collapsing them told the model "user rejected this edit" for a request nobody
    saw, which can make it abandon a correct plan.
    """
    session = Session(session_id="sess_test", workspace="/tmp/workspace")
    approval = session.create_approval("tool", {"tool": "bash"})

    decision = await session.wait_for_approval(approval.approval_id, timeout_seconds=0.01)

    assert decision is ApprovalDecision.TIMED_OUT
    assert decision is not ApprovalDecision.REJECTED
    assert approval.resolution == "timed_out"
    assert approval.to_dict()["status"] == "timed_out"
    # Still terminal: a late answer cannot revive an expired request.
    assert not session.resolve_approval(approval.approval_id, accepted=True)


@pytest.mark.asyncio
async def test_missing_approval_is_reported_separately() -> None:
    session = Session(session_id="sess_test", workspace="/tmp/workspace")

    assert await session.wait_for_approval("appr_nonexistent") is ApprovalDecision.MISSING


@pytest.mark.asyncio
async def test_run_cancellation_records_a_cancelled_resolution() -> None:
    """Cancellation is its own resolution, so a listing does not claim the user
    refused the request."""
    session = Session(session_id="sess_test", workspace="/tmp/workspace")
    approval = session.create_approval("edit", {"path": "a.py"})

    expired = session.expire_pending_approvals()

    assert [item.approval_id for item in expired] == [approval.approval_id]
    assert approval.resolution == "cancelled"
    assert approval.to_dict()["status"] == "cancelled"
    assert await session.wait_for_approval(approval.approval_id, timeout_seconds=1) is ApprovalDecision.REJECTED
