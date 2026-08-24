import pytest
from fastapi import HTTPException

from app.sessions.store import SessionStore
from tests.fakes import build_test_runtime


@pytest.mark.asyncio
async def test_approve_accept_all_sets_session_flag(tmp_path):
    from app.server import main as server

    store = SessionStore(path=tmp_path / "s.sqlite")
    runtime = build_test_runtime(tmp_path, sessions=store)
    session = store.create(workspace=str(tmp_path))
    approval = session.create_approval("edit", {"path": "a.py"})

    response = await server.approve(
        session.session_id,
        server.ApprovalRequest(approval_id=approval.approval_id, accept_all=True),
        runtime,
    )

    assert response["status"] == "accepted"
    assert session.auto_accept_edits is True
    assert approval.accepted is True


def test_agent_runtime_has_policy(tmp_path):
    assert build_test_runtime(tmp_path).agent.policy is not None


def test_run_coordinator_is_wired_by_the_composition_root(tmp_path):
    """The transport must reuse the runtime's coordinator rather than build its own.

    main.py previously rebuilt every service per request — including a fresh
    AgentLoop — while ApplicationRuntime had already wired them, leaving the
    runtime's own services dead code.
    """
    runtime = build_test_runtime(tmp_path)

    assert runtime.runs is not None
    assert runtime.runs.agent_loop.runtime is runtime.agent
    # Repeated access returns the same coordinator, not a per-call rebuild.
    assert runtime.runs is runtime.runs


@pytest.mark.asyncio
async def test_send_message_rejects_unconfigured_provider(tmp_path, monkeypatch):
    from app.server import main as server

    store = SessionStore(path=tmp_path / "s.sqlite")
    runtime = build_test_runtime(tmp_path, sessions=store)
    session = store.create(workspace=str(tmp_path))
    monkeypatch.setattr(runtime.agent.model_runtime.primary, "is_configured", lambda: False)

    with pytest.raises(HTTPException) as exc_info:
        await server.send_message(
            session.session_id,
            server.MessageRequest(message="test message", mode="default", workspace=str(tmp_path)),
            runtime,
        )

    assert exc_info.value.status_code == 400
    # No run may be enqueued as a side effect of the rejection.
    assert session.agent_queue.qsize() == 0


@pytest.mark.asyncio
async def test_two_runtimes_coexist_in_one_process(tmp_path):
    """The point of moving construction into the lifespan.

    While the runtime was a module global, one process could host exactly one
    configuration and tests had to monkeypatch shared state.
    """
    from app.server import main as server

    first = build_test_runtime(tmp_path / "a", sessions=SessionStore(path=tmp_path / "a.sqlite"))
    second = build_test_runtime(tmp_path / "b", sessions=SessionStore(path=tmp_path / "b.sqlite"))

    created_first = await server.create_session(
        server.CreateSessionRequest(workspace=str(tmp_path / "a")), first
    )
    created_second = await server.create_session(
        server.CreateSessionRequest(workspace=str(tmp_path / "b")), second
    )

    assert created_first.session_id != created_second.session_id
    # Each runtime sees only its own session.
    assert first.sessions.get(created_second.session_id) is None
    assert second.sessions.get(created_first.session_id) is None
    assert [row["session_id"] for row in first.sessions.list()] == [created_first.session_id]
    assert [row["session_id"] for row in second.sessions.list()] == [created_second.session_id]
