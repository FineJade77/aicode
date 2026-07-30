from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.agent.loop import AgentLoop
from app.agent.policy import PolicyEngine
from app.agent.types import AgentRuntime
from app.application.contracts import contract_descriptor
from app.config import Settings
from app.models.router import ModelRouter
from app.sessions.approvals import SessionApprovalBroker
from app.sessions.memory import InMemorySessionRepository
from app.system import SystemClock
from app.tools.runtime import DefaultToolRuntime
from app.tools.workspace import LocalWorkspaceRuntime
from tests.fakes import FakeProvider, text_turn

RUNTIME_APP = Path(__file__).resolve().parents[1] / "app"


class MemoryTrace:
    def __init__(self) -> None:
        self.path = Path("/dev/null")
        self.events: list[dict[str, Any]] = []

    def record(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        workspace: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "event_type": event_type,
                "session_id": session_id,
                "workspace": workspace,
                "data": data or {},
            }
        )

    def status(self) -> dict[str, Any]:
        return {"active": False, "queue_size": 0, "failed": 0}

    async def flush(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class Request:
    def __init__(self, workspace: Path) -> None:
        self.workspace = str(workspace)
        self.message = "Explain the current status"
        self.mode = "default"


def test_agent_import_has_no_transport_or_concrete_runtime_side_effects() -> None:
    code = """
import sys
from app.agent.loop import AgentLoop
forbidden = [
    name for name in sys.modules
    if name == "fastapi"
    or name.startswith("app.server")
    or name == "app.bootstrap"
    or name.startswith("app.sessions.store")
    or name.startswith("app.tools.runtime")
    or name.startswith("app.tools.workspace")
]
assert not forbidden, forbidden
"""
    subprocess.run([sys.executable, "-c", code], cwd=RUNTIME_APP.parent, check=True)


def test_application_contract_import_has_no_transport_or_bootstrap_side_effects() -> None:
    code = """
import sys
from app.application import TurnRequest
assert TurnRequest.__name__ == "TurnRequest"
forbidden = [
    name for name in sys.modules
    if name == "fastapi"
    or name.startswith("app.server")
    or name == "app.bootstrap"
]
assert not forbidden, forbidden
"""
    subprocess.run([sys.executable, "-c", code], cwd=RUNTIME_APP.parent, check=True)


def test_agent_and_application_do_not_import_transport_or_bootstrap() -> None:
    agent_forbidden = (
        "fastapi",
        "app.server",
        "app.bootstrap",
        "app.sessions.store",
        "app.tools.runtime",
        "app.tools.workspace",
    )
    for path in (RUNTIME_APP / "agent").glob("*.py"):
        imports = imported_modules(path)
        assert not [name for name in imports if name.startswith(agent_forbidden)], path

    for path in (RUNTIME_APP / "application").glob("*.py"):
        imports = imported_modules(path)
        assert not [
            name for name in imports if name.startswith(("fastapi", "app.server", "app.bootstrap"))
        ], path


@pytest.mark.asyncio
async def test_agent_runs_with_fake_model_and_in_memory_session(tmp_path: Path) -> None:
    trace = MemoryTrace()
    model = ModelRouter(primary=FakeProvider([text_turn("No changes needed")]), settings=Settings())
    runtime = AgentRuntime(
        model_runtime=model,
        trace=trace,
        policy=PolicyEngine(),
        tools=DefaultToolRuntime(),
        workspace=LocalWorkspaceRuntime(),
        clock=SystemClock(),
        approvals=SessionApprovalBroker(),
    )
    sessions = InMemorySessionRepository(clock=runtime.clock)
    session = sessions.create(str(tmp_path))

    await AgentLoop(runtime).run(session, Request(tmp_path))

    assert session.events.events_after(0)[-1]["type"] == "final"
    assert session.events.events_after(0)[-1]["summary"] == "No changes needed"
    assert any(event["event_type"] == "session.final" for event in trace.events)


def test_versioned_contract_describes_current_and_future_transports() -> None:
    contract = contract_descriptor("0.1.0")

    assert contract["contract_version"] == "2.0"
    assert contract["application"]["version"] == "2.0"
    assert contract["application"]["schema"] == "v2"
    assert contract["application"]["types"]["turn"] == "TurnRequest"
    assert contract["transports"]["http"]["status"] == "stable"
    assert contract["transports"]["sse"]["event_schema"] == "v2"
    assert contract["transports"]["stdio_jsonrpc"]["status"] == "planned"


def imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def test_importing_the_transport_does_not_build_a_runtime(tmp_path: Path) -> None:
    """Importing `app.server.main` must have no infrastructure side effects.

    A module-level `build_application_runtime(settings)` used to open SQLite and
    construct provider clients merely because something imported this module. The
    ASGI lifespan now owns construction, which is also what allows two
    differently configured runtimes in one process.
    """
    code = f"""
import os, pathlib
state = pathlib.Path({str(tmp_path)!r})
os.environ["AICODE_HOME"] = str(state)
os.environ["AICODE_SESSION_DB_PATH"] = str(state / "sessions.sqlite")
os.environ["AICODE_AUDIT_PATH"] = str(state / "audit.jsonl")

from app.server import main

assert not hasattr(main, "application_runtime"), "the module global must not come back"
assert getattr(main.app.state, "runtime", None) is None, "no runtime before the lifespan runs"
leaked = sorted(p.name for p in state.iterdir()) if state.exists() else []
assert leaked == [], leaked
"""
    subprocess.run([sys.executable, "-c", code], cwd=RUNTIME_APP.parent, check=True)


@pytest.mark.asyncio
async def test_lifespan_builds_and_releases_the_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICODE_HOME", str(tmp_path))
    monkeypatch.setenv("AICODE_SESSION_DB_PATH", str(tmp_path / "sessions.sqlite"))
    monkeypatch.setenv("AICODE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from app.server import main as server

    assert getattr(server.app.state, "runtime", None) is None
    async with server.lifespan(server.app):
        runtime = server.app.state.runtime
        assert runtime is not None
        assert runtime.session_service is not None

    assert server.app.state.runtime is None, "the lifespan must release the runtime on shutdown"
