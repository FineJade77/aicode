"""Background and long-running commands.

`bash` blocks for at most a few minutes, so a dev server, a watch build, or a
long compile could previously only be run by blocking the whole turn — which in
practice means not running them at all.
"""

import asyncio
import sys

import pytest
import pytest_asyncio

from app.execution.background import (
    MAX_BACKGROUND_PROCESSES,
    MAX_BUFFER_CHARS,
    BackgroundProcessError,
    BackgroundProcessManager,
)
from app.execution.service import ExecutionService
from app.tools.base import ToolContext
from app.tools.registry import DEFAULT_REGISTRY
from tests.process_helpers import (
    GROUP_KILL_TIMEOUT,
    assert_process_exits,
    grandchild_command,
    grandchild_pid,
    process_alive,
)


async def wait_for(predicate, *, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest_asyncio.fixture
async def manager():
    instance = BackgroundProcessManager()
    try:
        yield instance
    finally:
        await instance.stop_all()


@pytest.mark.asyncio
async def test_start_returns_immediately_for_a_command_that_never_exits(manager, tmp_path):
    """The whole point: a long-lived command must not block the caller."""
    started = asyncio.get_running_loop().time()
    entry = await manager.start(f"{sys.executable} -c 'import time; time.sleep(60)'", workspace=tmp_path)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 5.0, "starting a background command must not wait for it"
    assert entry.running
    assert entry.status() == "running"


@pytest.mark.asyncio
async def test_output_accumulates_and_reads_are_incremental(manager, tmp_path):
    entry = await manager.start(
        f"{sys.executable} -c 'import sys,time; print(\"first\", flush=True); time.sleep(30)'",
        workspace=tmp_path,
    )
    assert await wait_for(lambda: "first" in entry.buffer)

    first = manager.read(entry.handle_id)
    assert "first" in first.text
    assert first.status == "running"

    second = manager.read(entry.handle_id, after=first.next_offset)
    assert second.text == "", "a second read must return only what is new"


@pytest.mark.asyncio
async def test_an_exited_command_reports_its_code(manager, tmp_path):
    entry = await manager.start(f"{sys.executable} -c 'raise SystemExit(3)'", workspace=tmp_path)
    assert await wait_for(lambda: entry.exit_code is not None)

    result = manager.read(entry.handle_id)
    assert result.status == "exited"
    assert result.exit_code == 3


@pytest.mark.asyncio
async def test_stopping_kills_the_whole_process_group(manager, tmp_path):
    """A dev server that spawns workers must not leave orphans behind."""
    pid_path = tmp_path / "grandchild.pid"
    entry = await manager.start(
        f"{sys.executable} -c {grandchild_command(pid_path)!r}",
        workspace=tmp_path,
    )
    assert await wait_for(pid_path.exists, timeout=GROUP_KILL_TIMEOUT + 4.0)
    pid = grandchild_pid(pid_path)
    assert process_alive(pid)

    await manager.stop(entry.handle_id)

    await assert_process_exits(pid, "stopping a background command must kill its whole process group")
    assert entry.status() == "stopped"


@pytest.mark.asyncio
async def test_stop_all_reaps_everything(manager, tmp_path):
    entries = [
        await manager.start(f"{sys.executable} -c 'import time; time.sleep(60)'", workspace=tmp_path)
        for _ in range(3)
    ]
    pids = [entry.process.pid for entry in entries]

    await manager.stop_all()

    for pid in pids:
        await assert_process_exits(pid, "daemon shutdown must leave no background process running")
    assert manager.list() == []


@pytest.mark.asyncio
async def test_daemon_shutdown_path_reaps_background_commands(tmp_path):
    """`cancel_all` is what the lifespan calls, so it must cover these too."""
    service = ExecutionService()
    entry = await service.background.start(
        f"{sys.executable} -c 'import time; time.sleep(60)'", workspace=tmp_path
    )
    pid = entry.process.pid
    assert service.status()["background"] == 1

    await service.cancel_all()

    await assert_process_exits(pid, "cancel_all must reap background commands")
    assert service.status()["background"] == 0


@pytest.mark.asyncio
async def test_stopping_twice_is_harmless(manager, tmp_path):
    entry = await manager.start(f"{sys.executable} -c 'import time; time.sleep(60)'", workspace=tmp_path)
    await manager.stop(entry.handle_id)
    again = await manager.stop(entry.handle_id)
    assert again.status() in {"stopped", "exited"}


@pytest.mark.asyncio
async def test_an_unknown_handle_is_reported_not_ignored(manager):
    with pytest.raises(BackgroundProcessError):
        manager.read("bg_nope")
    with pytest.raises(BackgroundProcessError):
        await manager.stop("bg_nope")


@pytest.mark.asyncio
async def test_too_many_background_commands_is_refused(manager, tmp_path):
    for _ in range(MAX_BACKGROUND_PROCESSES):
        await manager.start(f"{sys.executable} -c 'import time; time.sleep(60)'", workspace=tmp_path)
    with pytest.raises(BackgroundProcessError, match="too many"):
        await manager.start(f"{sys.executable} -c 'import time; time.sleep(60)'", workspace=tmp_path)


@pytest.mark.asyncio
async def test_an_exited_command_frees_its_slot(manager, tmp_path):
    for _ in range(MAX_BACKGROUND_PROCESSES):
        entry = await manager.start(f"{sys.executable} -c 'pass'", workspace=tmp_path)
    assert await wait_for(lambda: all(not e.running for e in manager.list()))
    await manager.start(f"{sys.executable} -c 'import time; time.sleep(60)'", workspace=tmp_path)
    assert entry is not None


@pytest.mark.asyncio
async def test_a_missing_workspace_is_refused(manager, tmp_path):
    with pytest.raises(BackgroundProcessError):
        await manager.start("echo hi", workspace=tmp_path / "nope")


@pytest.mark.asyncio
async def test_overflowing_output_reports_the_gap_rather_than_hiding_it(manager, tmp_path):
    """A silent gap would read as contiguous output the model could reason about."""
    entry = await manager.start(f"{sys.executable} -c 'pass'", workspace=tmp_path)
    assert await wait_for(lambda: entry.exit_code is not None)
    entry.append("x" * (MAX_BUFFER_CHARS + 500))

    assert entry.dropped_chars == 500
    result = manager.read(entry.handle_id, after=0)
    assert result.dropped == 500
    assert len(result.text) == MAX_BUFFER_CHARS


def make_context(tmp_path, service, session=None):
    return ToolContext(workspace=tmp_path, execution=service, session=session, session_id="sess_1")


class FakeSession:
    def __init__(self):
        self.background_offsets: dict[str, int] = {}


@pytest.mark.asyncio
async def test_bash_background_returns_a_handle_and_tools_drive_it(tmp_path):
    service = ExecutionService()
    session = FakeSession()
    context = make_context(tmp_path, service, session)
    try:
        started = await DEFAULT_REGISTRY.run(
            "bash",
            {"command": f"{sys.executable} -c 'import sys,time; print(\"hello\", flush=True); time.sleep(30)'", "background": True},
            context,
        )
        assert started.success
        handle = started.data["handle"]
        assert handle and "read_output" in started.text

        assert await wait_for(lambda: "hello" in service.background.get(handle).buffer)

        first = await DEFAULT_REGISTRY.run("read_output", {"handle": handle}, context)
        assert first.success and "hello" in first.text

        second = await DEFAULT_REGISTRY.run("read_output", {"handle": handle}, context)
        assert "no new output" in second.text, "the session cursor must advance between reads"

        listed = await DEFAULT_REGISTRY.run("read_output", {}, context)
        assert handle in listed.text

        stopped = await DEFAULT_REGISTRY.run("stop_command", {"handle": handle}, context)
        assert stopped.success and stopped.data["status"] in {"stopped", "exited"}
    finally:
        await service.cancel_all()


@pytest.mark.asyncio
async def test_read_cursors_are_per_session(tmp_path):
    """Two sessions watching one command must not consume each other's output."""
    service = ExecutionService()
    one, two = FakeSession(), FakeSession()
    try:
        started = await DEFAULT_REGISTRY.run(
            "bash",
            {"command": f"{sys.executable} -c 'import sys,time; print(\"shared\", flush=True); time.sleep(30)'", "background": True},
            make_context(tmp_path, service, one),
        )
        handle = started.data["handle"]
        assert await wait_for(lambda: "shared" in service.background.get(handle).buffer)

        first = await DEFAULT_REGISTRY.run("read_output", {"handle": handle}, make_context(tmp_path, service, one))
        other = await DEFAULT_REGISTRY.run("read_output", {"handle": handle}, make_context(tmp_path, service, two))

        assert "shared" in first.text
        assert "shared" in other.text, "the second session must still see the output"
    finally:
        await service.cancel_all()


@pytest.mark.asyncio
async def test_background_is_refused_on_the_docker_backend(tmp_path):
    """Refusing beats quietly running an untrusted workspace's server on the host."""
    service = ExecutionService()
    context = ToolContext(
        workspace=tmp_path,
        execution=service,
        session=FakeSession(),
        trust_level="untrusted",
        bash_backend="docker",
    )
    result = await DEFAULT_REGISTRY.run("bash", {"command": "sleep 60", "background": True}, context)
    assert not result.success
    assert "Docker backend" in result.error
    assert service.status()["background"] == 0


@pytest.mark.asyncio
async def test_stopping_an_unknown_handle_reports_an_error(tmp_path):
    service = ExecutionService()
    result = await DEFAULT_REGISTRY.run(
        "stop_command", {"handle": "bg_missing"}, make_context(tmp_path, service, FakeSession())
    )
    assert not result.success
    assert "bg_missing" in result.error
