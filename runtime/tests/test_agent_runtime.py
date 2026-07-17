import asyncio
from pathlib import Path

import pytest

from app.agent.loop import run_agent_safely
from app.agent.patch_flow import MAX_PATCH_DIFF_BYTES, propose_patch
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.models.provider import ModelResponse
from app.server.main import MessageRequest
from app.sessions.store import Session
from app.tools.patch import PatchProposal
from app.tools.router import ToolRouter


class UnconfiguredPrimary:
    def is_configured(self) -> bool:
        return False


class BrokenModelRouter:
    primary = UnconfiguredPrimary()

    async def complete(self, request):
        raise RuntimeError("model unavailable")


class ConfiguredPrimary:
    def is_configured(self) -> bool:
        return True


class CoderPatchModelRouter:
    primary = ConfiguredPrimary()

    def __init__(self) -> None:
        self.purposes: list[str] = []

    async def complete(self, request):
        self.purposes.append(request.purpose)
        if request.purpose == "planner":
            return ModelResponse(text='{"action":"finish","reason":"context collected"}', model="test-planner", provider="test")
        if request.purpose == "coder":
            return ModelResponse(
                text='{"action":"patch","operation":"replace","path":"README.md","old_text":"old value","new_text":"new value","reason":"update requested text"}',
                model="test-coder",
                provider="test",
            )
        return ModelResponse(text="done", model="test-summary", provider="test")


class MultiFileCoderPatchModelRouter:
    primary = ConfiguredPrimary()

    def __init__(self) -> None:
        self.purposes: list[str] = []

    async def complete(self, request):
        self.purposes.append(request.purpose)
        if request.purpose == "planner":
            return ModelResponse(text='{"action":"finish","reason":"context collected"}', model="test-planner", provider="test")
        if request.purpose == "coder":
            return ModelResponse(
                text=(
                    '{"action":"patch","operations":['
                    '{"operation":"replace","path":"README.md","old_text":"old value","new_text":"new value"},'
                    '{"operation":"create","path":"TODO.md","content":"first task"}'
                    '],"reason":"update multiple files"}'
                ),
                model="test-coder",
                provider="test",
            )
        return ModelResponse(text="", model="test-summary", provider="stub")


class ContextAwareCoderPatchModelRouter:
    primary = ConfiguredPrimary()

    def __init__(self) -> None:
        self.coder_messages = []

    async def complete(self, request):
        if request.purpose == "planner":
            return ModelResponse(text='{"action":"finish","reason":"context collected"}', model="test-planner", provider="test")
        if request.purpose == "coder":
            self.coder_messages = request.messages
            return ModelResponse(
                text='{"action":"patch","operation":"replace","path":"src/calc.py","old_text":"return a - b","new_text":"return a + b","reason":"fix add"}',
                model="test-coder",
                provider="test",
            )
        return ModelResponse(text="", model="test-summary", provider="stub")


class DependencyAwareCoderPatchModelRouter:
    primary = ConfiguredPrimary()

    def __init__(self) -> None:
        self.coder_messages = []

    async def complete(self, request):
        if request.purpose == "planner":
            return ModelResponse(text='{"action":"finish","reason":"context collected"}', model="test-planner", provider="test")
        if request.purpose == "coder":
            self.coder_messages = request.messages
            return ModelResponse(
                text='{"action":"patch","operation":"replace","path":"src/service.py","old_text":"return helper() - 1","new_text":"return helper() + 1","reason":"fix compute"}',
                model="test-coder",
                provider="test",
            )
        return ModelResponse(text="", model="test-summary", provider="stub")


class HardenedPatchModelRouter:
    primary = ConfiguredPrimary()

    async def complete(self, request):
        if request.purpose == "planner":
            return ModelResponse(text='{"action":"finish","reason":"context collected"}', model="test-planner", provider="test")
        if request.purpose == "coder":
            return ModelResponse(
                text=(
                    '{"action":"patch","schema_version":1,"operations":['
                    '{"operation":"replace","path":"README.md","old_text":"old value","new_text":"new value"},'
                    '{"operation":"append","path":"README.md","text":"second line"},'
                    '{"operation":"delete","path":"OLD.md"},'
                    '{"operation":"rename","path":"old.py","new_path":"new.py"}'
                    '],"reason":"exercise hardened patch flow"}'
                ),
                model="test-coder",
                provider="test",
            )
        return ModelResponse(text="", model="test-summary", provider="stub")


class RepairingCoderModelRouter:
    primary = ConfiguredPrimary()

    def __init__(self) -> None:
        self.purposes: list[str] = []
        self.coder_calls = 0

    async def complete(self, request):
        self.purposes.append(request.purpose)
        if request.purpose == "planner":
            return ModelResponse(text='{"action":"finish","reason":"context collected"}', model="test-planner", provider="test")
        if request.purpose == "coder":
            self.coder_calls += 1
            if self.coder_calls == 1:
                return ModelResponse(
                    text='{"action":"patch","operation":"replace","path":"calc.py","old_text":"return a - b","new_text":"return 0","reason":"first attempt"}',
                    model="test-coder",
                    provider="test",
                )
            return ModelResponse(
                text='{"action":"patch","operation":"replace","path":"calc.py","old_text":"return 0","new_text":"return a + b","reason":"repair failing test"}',
                model="test-coder",
                provider="test",
            )
        return ModelResponse(text="done", model="test-summary", provider="test")


@pytest.mark.asyncio
async def test_run_agent_safely_emits_final_on_model_error(tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="hello", mode="default", workspace=str(tmp_path), language="zh-CN")
    runtime = AgentRuntime(
        model_router=BrokenModelRouter(),
        tools=ToolRouter(),
        audit=AuditLogger(tmp_path / "audit.jsonl"),
    )

    await run_agent_safely(session, request, runtime)

    events = []
    while not session.events.empty():
        events.append(await session.events.get())

    assert any(event["type"] == "error" for event in events)
    assert events[-1]["type"] == "final"
    assert "Agent 执行失败" in events[-1]["summary"]


@pytest.mark.asyncio
async def test_run_agent_uses_coder_patch_after_context_and_requires_approval(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("old value\n", encoding="utf-8")
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="修复 README.md 里的旧文案", mode="default", workspace=str(tmp_path), language="zh-CN")
    model_router = CoderPatchModelRouter()
    runtime = AgentRuntime(
        model_router=model_router,
        tools=ToolRouter(),
        audit=AuditLogger(tmp_path / "audit.jsonl"),
    )

    task = asyncio.create_task(run_agent_safely(session, request, runtime))
    events = []
    while True:
        event = await asyncio.wait_for(session.events.get(), timeout=5)
        events.append(event)
        if event["type"] == "approval.requested" and event.get("kind") == "patch":
            assert (tmp_path / "README.md").read_text(encoding="utf-8") == "old value\n"
            assert session.resolve_approval(event["approval_id"], accepted=True)
        if event["type"] == "final":
            break
    await asyncio.wait_for(task, timeout=5)

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "new value\n"
    assert "coder" in model_router.purposes
    assert any(event["type"] == "patch.preview" for event in events)
    assert any(event["type"] == "patch.applied" for event in events)


@pytest.mark.asyncio
async def test_run_agent_uses_multi_file_coder_patch_with_single_approval(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("old value\n", encoding="utf-8")
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="更新 README 并创建 TODO", mode="default", workspace=str(tmp_path), language="zh-CN")
    model_router = MultiFileCoderPatchModelRouter()
    runtime = AgentRuntime(
        model_router=model_router,
        tools=ToolRouter(),
        audit=AuditLogger(tmp_path / "audit.jsonl"),
    )

    task = asyncio.create_task(run_agent_safely(session, request, runtime))
    events = []
    approval_count = 0
    while True:
        event = await asyncio.wait_for(session.events.get(), timeout=5)
        events.append(event)
        if event["type"] == "approval.requested" and event.get("kind") == "patch":
            approval_count += 1
            assert (tmp_path / "README.md").read_text(encoding="utf-8") == "old value\n"
            assert not (tmp_path / "TODO.md").exists()
            assert session.resolve_approval(event["approval_id"], accepted=True)
        if event["type"] == "final":
            break
    await asyncio.wait_for(task, timeout=5)

    assert approval_count == 1
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "new value\n"
    assert (tmp_path / "TODO.md").read_text(encoding="utf-8") == "first task\n"
    previews = [event for event in events if event["type"] == "patch.preview"]
    assert len(previews) == 1
    assert previews[0]["files"] == ["README.md", "TODO.md"]
    assert "--- a/README.md" in previews[0]["diff"]
    assert "--- /dev/null" in previews[0]["diff"]


@pytest.mark.asyncio
async def test_run_agent_reads_located_context_before_coder_patch(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="修复 add 函数", mode="default", workspace=str(tmp_path), language="zh-CN")
    model_router = ContextAwareCoderPatchModelRouter()
    runtime = AgentRuntime(
        model_router=model_router,
        tools=ToolRouter(),
        audit=AuditLogger(tmp_path / ".aicode" / "audit.jsonl"),
    )

    task = asyncio.create_task(run_agent_safely(session, request, runtime))
    events = []
    while True:
        event = await asyncio.wait_for(session.events.get(), timeout=10)
        events.append(event)
        if event["type"] == "approval.requested" and event.get("kind") == "patch":
            assert session.resolve_approval(event["approval_id"], accepted=True)
        if event["type"] == "final":
            break
    await asyncio.wait_for(task, timeout=10)

    assert (tmp_path / "src" / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert any(event["type"] == "tool.output" and event.get("tool") == "search_text" for event in events)
    assert any(event["type"] == "tool.output" and event.get("tool") == "read_file" and event["data"]["path"] == "src/calc.py" for event in events)
    assert "def add" in model_router.coder_messages[1]["content"]


@pytest.mark.asyncio
async def test_run_agent_reads_related_test_context_before_coder_patch(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()
    (src_dir / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tests_dir / "test_calc.py").write_text("from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="修复 src/calc.py 的 add 函数", mode="default", workspace=str(tmp_path), language="zh-CN")
    model_router = ContextAwareCoderPatchModelRouter()
    runtime = AgentRuntime(
        model_router=model_router,
        tools=ToolRouter(),
        audit=AuditLogger(tmp_path / ".aicode" / "audit.jsonl"),
    )

    task = asyncio.create_task(run_agent_safely(session, request, runtime))
    events = []
    while True:
        event = await asyncio.wait_for(session.events.get(), timeout=10)
        events.append(event)
        if event["type"] == "approval.requested" and event.get("kind") == "patch":
            assert session.resolve_approval(event["approval_id"], accepted=True)
        if event["type"] == "final":
            break
    await asyncio.wait_for(task, timeout=10)

    assert any(
        event["type"] == "tool.output" and event.get("tool") == "find_files" and event["data"]["query"] == "test_calc.py"
        for event in events
    )
    assert any(
        event["type"] == "tool.output" and event.get("tool") == "read_file" and event["data"]["path"] == "tests/test_calc.py"
        for event in events
    )
    assert "tests/test_calc.py" in model_router.coder_messages[1]["content"]
    assert "assert add(1, 2) == 3" in model_router.coder_messages[1]["content"]


@pytest.mark.asyncio
async def test_run_agent_reads_dependency_context_before_coder_patch(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "service.py").write_text(
        "from .utils import helper\n\n\ndef compute():\n    return helper() - 1\n",
        encoding="utf-8",
    )
    (src_dir / "utils.py").write_text("def helper():\n    return 41\n", encoding="utf-8")
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="修复 src/service.py 的 compute 函数", mode="default", workspace=str(tmp_path), language="zh-CN")
    model_router = DependencyAwareCoderPatchModelRouter()
    runtime = AgentRuntime(
        model_router=model_router,
        tools=ToolRouter(),
        audit=AuditLogger(tmp_path / ".aicode" / "audit.jsonl"),
    )

    task = asyncio.create_task(run_agent_safely(session, request, runtime))
    events = []
    while True:
        event = await asyncio.wait_for(session.events.get(), timeout=10)
        events.append(event)
        if event["type"] == "approval.requested" and event.get("kind") == "patch":
            assert session.resolve_approval(event["approval_id"], accepted=True)
        if event["type"] == "final":
            break
    await asyncio.wait_for(task, timeout=10)

    assert (tmp_path / "src" / "service.py").read_text(encoding="utf-8").endswith("    return helper() + 1\n")
    assert any(
        event["type"] == "tool.output" and event.get("tool") == "find_files" and event["data"]["query"] == "src/utils.py"
        for event in events
    )
    assert any(
        event["type"] == "tool.output" and event.get("tool") == "read_file" and event["data"]["path"] == "src/utils.py"
        for event in events
    )
    assert "src/utils.py" in model_router.coder_messages[1]["content"]
    assert "def helper" in model_router.coder_messages[1]["content"]


@pytest.mark.asyncio
async def test_run_agent_applies_hardened_multi_operation_patch(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("old value\n", encoding="utf-8")
    (tmp_path / "OLD.md").write_text("remove me\n", encoding="utf-8")
    (tmp_path / "old.py").write_text("print('ok')\n", encoding="utf-8")
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="修改多操作 patch", mode="default", workspace=str(tmp_path), language="zh-CN")
    runtime = AgentRuntime(
        model_router=HardenedPatchModelRouter(),
        tools=ToolRouter(),
        audit=AuditLogger(tmp_path / "audit.jsonl"),
    )

    task = asyncio.create_task(run_agent_safely(session, request, runtime))
    events = []
    approval_count = 0
    while True:
        event = await asyncio.wait_for(session.events.get(), timeout=5)
        events.append(event)
        if event["type"] == "approval.requested" and event.get("kind") == "patch":
            approval_count += 1
            assert session.resolve_approval(event["approval_id"], accepted=True)
        if event["type"] == "final":
            break
    await asyncio.wait_for(task, timeout=5)

    assert approval_count == 1
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "new value\nsecond line\n"
    assert not (tmp_path / "OLD.md").exists()
    assert not (tmp_path / "old.py").exists()
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "print('ok')\n"
    preview = next(event for event in events if event["type"] == "patch.preview")
    assert preview["files"] == ["old.py", "new.py", "README.md", "OLD.md"]
    assert "rename from old.py" in preview["diff"]
    assert "+++ /dev/null" in preview["diff"]


@pytest.mark.asyncio
async def test_propose_patch_rejects_large_diff_before_approval(tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="large patch", mode="default", workspace=str(tmp_path), language="zh-CN")
    runtime = AgentRuntime(
        model_router=CoderPatchModelRouter(),
        tools=ToolRouter(),
        audit=AuditLogger(tmp_path / "audit.jsonl"),
    )
    proposal = PatchProposal(path="README.md", diff="+" * (MAX_PATCH_DIFF_BYTES + 1), new_content="")

    outcome = await propose_patch(session, request, proposal, operation="replace", runtime=runtime)

    assert outcome["status"] == "error"
    assert "patch diff too large" in outcome["reason"]
    event = await asyncio.wait_for(session.events.get(), timeout=1)
    assert event["type"] == "tool.error"
    assert session.approvals == {}


@pytest.mark.asyncio
async def test_run_agent_repairs_once_after_failed_verification(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="修复 calc.py 的 add 函数", mode="default", workspace=str(tmp_path), language="zh-CN")
    model_router = RepairingCoderModelRouter()
    runtime = AgentRuntime(
        model_router=model_router,
        tools=ToolRouter(),
        audit=AuditLogger(tmp_path / "audit.jsonl"),
    )

    task = asyncio.create_task(run_agent_safely(session, request, runtime))
    events = []
    approval_count = 0
    while True:
        event = await asyncio.wait_for(session.events.get(), timeout=30)
        events.append(event)
        if event["type"] == "approval.requested" and event.get("kind") == "patch":
            approval_count += 1
            assert session.resolve_approval(event["approval_id"], accepted=True)
        if event["type"] == "final":
            break
    await asyncio.wait_for(task, timeout=30)

    assert approval_count == 2
    assert model_router.coder_calls == 2
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert any(event["type"] == "verification.analysis" for event in events)
    assert any(event["type"] == "verification.repair.started" for event in events)
    completed = [event for event in events if event["type"] == "verification.completed"]
    assert [event["success"] for event in completed] == [False, True]
