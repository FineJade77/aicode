from pathlib import Path

import pytest

from app.agent.types import AgentRuntime
from app.application.contracts import (
    CompactionReceipt,
    RunControl,
    RunReceipt,
    SessionSnapshot,
    TurnRequest,
)
from app.application.services import ContextService, RunCoordinator, SessionService
from app.audit.logger import AuditLogger
from app.config import Settings
from app.models.router import ModelRouter
from app.sessions.memory import InMemorySessionRepository
from app.tools.workspace import LocalWorkspaceRuntime
from tests.fakes import FakeProvider


@pytest.mark.asyncio
async def test_session_service_exposes_snapshot_and_binds_turn_contract(tmp_path: Path) -> None:
    trace = AuditLogger(path=tmp_path / "audit.jsonl")
    sessions = InMemorySessionRepository()
    service = SessionService(sessions, trace, LocalWorkspaceRuntime())

    created = await service.create(str(tmp_path))
    turn = TurnRequest(
        message="inspect",
        mode="chat",
        workspace=str(tmp_path),
        model="fixture-model",
    )
    bound = service.bind_turn(service.require(created.session_id), turn)

    assert isinstance(created, SessionSnapshot)
    assert isinstance(service.get(created.session_id), SessionSnapshot)
    assert isinstance(service.list()[0], SessionSnapshot)
    assert bound.workspace == created.workspace
    assert bound.model == "fixture-model"
    assert "language" not in created.to_dict()
    assert "language" not in bound.to_dict()


@pytest.mark.asyncio
async def test_run_coordinator_returns_named_run_contracts(tmp_path: Path) -> None:
    trace = AuditLogger(path=tmp_path / "audit.jsonl")
    sessions = InMemorySessionRepository()
    session = sessions.create(str(tmp_path))

    class ImmediateLoop:
        async def run(self, target_session, _turn: TurnRequest) -> None:
            await target_session.events.put({"type": "final", "summary": "done"})

    coordinator = RunCoordinator(
        ModelRouter(primary=FakeProvider([]), settings=Settings()),
        trace,
        ImmediateLoop(),
    )
    receipt = await coordinator.submit(
        session,
        TurnRequest(
            message="inspect",
            mode="chat",
            workspace=str(tmp_path),
        ),
    )
    assert session.agent_runner_task is not None
    await session.agent_runner_task
    control = await coordinator.cancel(session)

    assert isinstance(receipt, RunReceipt)
    assert receipt.status == "accepted"
    assert isinstance(control, RunControl)
    assert control.status == "idle"


@pytest.mark.asyncio
async def test_context_service_returns_compaction_contract_for_in_memory_adapter(tmp_path: Path) -> None:
    trace = AuditLogger(path=tmp_path / "audit.jsonl")
    sessions = InMemorySessionRepository()
    session = sessions.create(str(tmp_path))
    for index in range(5):
        session.append_message({"role": "user", "content": f"constraint-{index}"})
    service = ContextService(AgentRuntime(model_runtime=None, trace=trace), trace)

    result = await service.compact(session)

    assert isinstance(result, CompactionReceipt)
    assert result.status == "compacted"
    assert result.compaction is not None
    assert result.compaction["session_id"] == session.session_id


@pytest.mark.asyncio
async def test_session_service_fork_returns_a_new_snapshot(tmp_path: Path) -> None:
    trace = AuditLogger(path=tmp_path / "audit.jsonl")
    sessions = InMemorySessionRepository()
    service = SessionService(sessions, trace, LocalWorkspaceRuntime())
    created = await service.create(str(tmp_path))
    source = service.require(created.session_id)
    source.append_message({"role": "user", "content": "first"})
    source.append_message({"role": "assistant", "content": "second"})

    forked = await service.fork(created.session_id)

    assert isinstance(forked, SessionSnapshot)
    assert forked.session_id != created.session_id
    assert [m["content"] for m in forked.messages] == ["first", "second"]


@pytest.mark.asyncio
async def test_session_service_fork_rejects_an_unknown_session(tmp_path: Path) -> None:
    from app.application.errors import NotFound

    service = SessionService(InMemorySessionRepository(), AuditLogger(path=tmp_path / "a.jsonl"), LocalWorkspaceRuntime())
    with pytest.raises(NotFound):
        await service.fork("sess_missing")
