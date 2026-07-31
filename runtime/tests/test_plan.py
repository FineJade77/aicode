from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.policy import PolicyEngine
from app.agent.session import MAX_PLAN_ITEMS, PlanItem
from app.sessions.store import SessionStore, parse_plan
from app.tools.base import ToolContext
from app.tools.registry import DEFAULT_REGISTRY


def context_for(session, tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=tmp_path, session=session)


async def update(session, tmp_path: Path, items):
    return await DEFAULT_REGISTRY.run("update_plan", {"items": items}, context_for(session, tmp_path))


@pytest.mark.asyncio
async def test_plan_is_replaced_wholesale(tmp_path: Path) -> None:
    """Whole replacement rather than incremental edits: an incremental API would
    require the model to keep stable item ids across turns."""
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))

    await update(session, tmp_path, [{"text": "a", "status": "pending"}, {"text": "b", "status": "pending"}])
    result = await update(session, tmp_path, [{"text": "b", "status": "done"}])

    assert result.success is True
    assert [item.to_dict() for item in session.plan] == [{"text": "b", "status": "done"}]


@pytest.mark.asyncio
async def test_plan_survives_a_daemon_restart(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite"
    store = SessionStore(path=db)
    session = store.create(workspace=str(tmp_path))
    await update(session, tmp_path, [{"text": "keep me", "status": "in_progress"}])
    store._close_connection()

    restored = SessionStore(path=db).get(session.session_id)

    assert restored is not None
    assert [item.to_dict() for item in restored.plan] == [{"text": "keep me", "status": "in_progress"}]
    assert restored.to_dict()["plan"] == [{"text": "keep me", "status": "in_progress"}]


@pytest.mark.asyncio
async def test_empty_plan_clears_it(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))
    await update(session, tmp_path, [{"text": "a", "status": "pending"}])

    result = await update(session, tmp_path, [])

    assert result.success is True
    assert session.plan == []
    assert "cleared" in result.text


@pytest.mark.asyncio
async def test_invalid_status_is_rejected_without_touching_the_plan(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))
    await update(session, tmp_path, [{"text": "keep", "status": "pending"}])

    result = await update(session, tmp_path, [{"text": "x", "status": "finished"}])

    assert result.success is False
    assert "finished" in result.error
    assert [item.text for item in session.plan] == ["keep"], "a rejected update must not clobber the plan"


@pytest.mark.asyncio
async def test_empty_text_and_oversized_plans_are_rejected(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))

    blank = await update(session, tmp_path, [{"text": "   ", "status": "pending"}])
    assert blank.success is False

    oversized = await update(
        session, tmp_path, [{"text": f"step {i}", "status": "pending"} for i in range(MAX_PLAN_ITEMS + 1)]
    )
    assert oversized.success is False
    assert str(MAX_PLAN_ITEMS) in oversized.error


@pytest.mark.asyncio
async def test_update_plan_needs_no_approval_but_is_not_parallelisable(tmp_path: Path) -> None:
    """The two axes differ here.

    `read_only=False` keeps it out of the parallel group (it mutates session
    state), while `approval="none"` keeps it out of the approval prompt (it
    touches nothing outside the session).
    """
    spec = DEFAULT_REGISTRY.spec_for("update_plan")
    assert spec is not None
    assert spec.read_only is False
    assert spec.approval == "none"
    assert PolicyEngine().gate("update_plan", {}, spec=spec).verdict == "allow"


def test_update_plan_is_denied_in_read_only_modes() -> None:
    """The read-only-mode boundary still applies: approval="none" is checked
    after it, not before."""
    spec = DEFAULT_REGISTRY.spec_for("update_plan")
    engine = PolicyEngine()
    for mode in ("review", "explain", "commit_message"):
        assert engine.gate("update_plan", {}, mode=mode, spec=spec).verdict == "deny", mode
    assert "update_plan" not in {s["name"] for s in DEFAULT_REGISTRY.schemas_for_mode("review")}


def test_persisted_plan_survives_malformed_entries() -> None:
    """A plan is model-authored display state, so one unreadable entry must not
    fail the whole session load."""
    assert parse_plan('[{"text":"ok","status":"done"},{"text":"","status":"done"},"junk",{"status":"done"}]') == [
        PlanItem(text="ok", status="done")
    ]
    assert parse_plan("not json") == []
    assert parse_plan(None) == []
