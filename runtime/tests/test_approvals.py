import asyncio

import pytest

from app.sessions.store import Session


@pytest.mark.asyncio
async def test_session_approval_can_be_resolved() -> None:
    session = Session(session_id="sess_test", workspace="/tmp/workspace", language="zh-CN")
    approval = session.create_approval("patch", {"path": "README.md"})

    async def resolve_later() -> None:
        await asyncio.sleep(0.01)
        assert session.resolve_approval(approval.approval_id, accepted=True)

    asyncio.create_task(resolve_later())

    assert await session.wait_for_approval(approval.approval_id, timeout_seconds=1) is True


@pytest.mark.asyncio
async def test_session_approval_rejects_duplicate_resolution() -> None:
    session = Session(session_id="sess_test", workspace="/tmp/workspace", language="zh-CN")
    approval = session.create_approval("patch", {"path": "README.md"})

    assert session.resolve_approval(approval.approval_id, accepted=False)
    assert not session.resolve_approval(approval.approval_id, accepted=True)
    assert await session.wait_for_approval(approval.approval_id, timeout_seconds=1) is False
