import pytest

from app.server import main as server


@pytest.mark.asyncio
async def test_approve_accept_all_sets_session_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("AICODE_SESSION_DB_PATH", str(tmp_path / "s.sqlite"))
    from app.sessions.store import SessionStore

    store = SessionStore(path=tmp_path / "s.sqlite")
    monkeypatch.setattr(server, "store", store)
    session = store.create(workspace=str(tmp_path), language="zh-CN")
    approval = session.create_approval("edit", {"path": "a.py"})

    response = await server.approve(session.session_id, server.ApprovalRequest(approval_id=approval.approval_id, accept_all=True))
    assert response["status"] == "accepted"
    assert session.auto_accept_edits is True
    assert approval.accepted is True


def test_agent_runtime_has_policy():
    assert server.agent_runtime.policy is not None


def test_run_agent_uses_v2_loop():
    import inspect

    source = inspect.getsource(server.run_agent)
    assert "run_turn_safely" in source
