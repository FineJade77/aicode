import shlex

import pytest

from app.tools import registry
from app.tools.base import ToolContext
from app.tools.command import CommandResult
from app.tools.registry import resolve_bash_backend, run_tool
from tests.process_helpers import assert_process_exits, grandchild_pid


@pytest.mark.asyncio
async def test_bash_runs_command(tmp_path):
    (tmp_path / "hello.txt").write_text("x", encoding="utf-8")
    result = await run_tool("bash", {"command": "ls"}, ToolContext(workspace=tmp_path))
    assert result.success
    assert isinstance(result.duration_ms, int)
    assert result.text.startswith("exit=0")
    assert "hello.txt" in result.text


@pytest.mark.asyncio
async def test_bash_nonzero_exit(tmp_path):
    result = await run_tool("bash", {"command": "false"}, ToolContext(workspace=tmp_path))
    assert not result.success
    assert "exit=1" in result.text


@pytest.mark.asyncio
async def test_bash_timeout(tmp_path):
    result = await run_tool("bash", {"command": "sleep 5", "timeout": 1}, ToolContext(workspace=tmp_path))
    assert not result.success
    assert "timed out" in result.error
    assert result.data["timed_out"] is True
    assert isinstance(result.duration_ms, int)


@pytest.mark.asyncio
async def test_bash_timeout_kills_grandchild_process(tmp_path):
    """A process spawned by `sh -c` is a grandchild of run_bash.

    Killing only the direct child on timeout would let the reparented grandchild
    keep running. The pid is recorded so this asserts the process is actually
    gone, rather than inferring it from a marker file that would also be absent
    if the grandchild had never started.
    """
    pid_path = tmp_path / "grandchild.pid"
    command = f"sleep 60 & echo $! > {shlex.quote(str(pid_path))} ; sleep 60"
    result = await run_tool(
        "bash", {"command": command, "timeout": 1}, ToolContext(workspace=tmp_path)
    )

    assert not result.success
    assert "timed out" in result.error
    await assert_process_exits(grandchild_pid(pid_path), "grandchild process leaked past the tool timeout")


@pytest.mark.asyncio
async def test_bash_does_not_inherit_provider_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AICODE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    result = await run_tool(
        "bash",
        {"command": "printf %s \"$OPENAI_API_KEY\""},
        ToolContext(workspace=tmp_path, trust_level="trusted"),
    )

    assert result.success
    assert "must-not-leak" not in result.text


@pytest.mark.parametrize(
    ("configured", "trust_level", "expected"),
    [
        # auto: trust decides. This is the default posture.
        ("auto", "trusted", "host"),
        ("auto", "untrusted", "docker"),
        ("auto", "unspecified", "docker"),
        # Explicit settings ignore trust in both directions.
        ("host", "untrusted", "host"),
        ("docker", "trusted", "docker"),
    ],
)
def test_resolve_bash_backend(configured, trust_level, expected):
    assert resolve_bash_backend(configured, trust_level) == expected


@pytest.mark.asyncio
async def test_untrusted_workspace_bash_is_routed_to_docker(tmp_path, monkeypatch):
    """An untrusted workspace must not execute Agent commands on the host."""
    captured = {}

    async def fake_run_shell_command(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return CommandResult(command=[command], returncode=0, stdout="", backend=kwargs["backend"], status="succeeded")

    monkeypatch.setattr(registry, "run_shell_command", fake_run_shell_command)
    monkeypatch.setattr(registry, "docker_available", lambda: True)

    result = await run_tool(
        "bash",
        {"command": "ls"},
        ToolContext(workspace=tmp_path, trust_level="untrusted", bash_backend="auto"),
    )

    assert result.success
    assert captured["backend"] == "docker"
    # Resource caps must be attached for the sandbox, not left at defaults.
    assert captured["limits"] is not None


@pytest.mark.asyncio
async def test_trusted_workspace_bash_stays_on_host(tmp_path, monkeypatch):
    captured = {}

    async def fake_run_shell_command(command, **kwargs):
        captured.update(kwargs)
        return CommandResult(command=[command], returncode=0, backend=kwargs["backend"], status="succeeded")

    monkeypatch.setattr(registry, "run_shell_command", fake_run_shell_command)

    result = await run_tool(
        "bash",
        {"command": "ls"},
        ToolContext(workspace=tmp_path, trust_level="trusted", bash_backend="auto"),
    )

    assert result.success
    assert captured["backend"] == "host"
    assert captured["limits"] is None


@pytest.mark.asyncio
async def test_bash_fails_loudly_when_sandbox_is_unavailable(tmp_path, monkeypatch):
    """The core assertion of the sandbox routing work.

    When a command is routed to Docker but Docker is missing, aicode must refuse
    and tell the user how to proceed. Falling back to host execution would remove
    the very boundary the routing exists to enforce, silently.
    """
    executed = False

    async def fail_if_called(*args, **kwargs):
        nonlocal executed
        executed = True
        raise AssertionError("command must not reach any execution backend")

    monkeypatch.setattr(registry, "run_shell_command", fail_if_called)
    monkeypatch.setattr(registry, "docker_available", lambda: False)

    result = await run_tool(
        "bash",
        {"command": "ls"},
        ToolContext(workspace=tmp_path, trust_level="untrusted", bash_backend="auto"),
    )

    assert not result.success
    assert executed is False
    assert result.data["backend"] == "docker"
    assert result.data["status"] == "unavailable"
    assert "aicode project trust add" in result.error
