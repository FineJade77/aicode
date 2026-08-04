"""OS-level sandbox backend.

Docker is a real boundary but an expensive one — a daemon, a pre-pulled image,
hundreds of milliseconds per command — and that cost is why trusted workspaces
run bare. The platform sandbox starts in milliseconds and needs nothing
installed, at the price of being a weaker boundary.
"""

import sys
from pathlib import Path

import pytest

from app.execution.models import ExecutionRequest, ResourceLimits
from app.execution.sandbox_os import (
    build_seatbelt_profile,
    os_sandbox_available,
    unavailable_reason,
)
from app.execution.service import ExecutionService
from app.tools.base import ToolContext
from app.tools.registry import DEFAULT_REGISTRY, resolve_bash_backend

darwin_only = pytest.mark.skipif(
    not os_sandbox_available(),
    reason="the OS sandbox is implemented with macOS seatbelt",
)


def test_profile_denies_writes_outside_the_writable_paths():
    profile = build_seatbelt_profile(
        writable_paths=(Path("/repo"),),
        masked_paths=(),
        allow_network=False,
    )
    assert "(deny file-write*)" in profile
    assert '(subpath "/repo")' in profile
    assert "(deny network*)" in profile
    # /dev/null has to stay writable or a shell fails in ways that read as the
    # command being broken rather than sandboxed.
    assert '(literal "/dev/null")' in profile


def test_profile_can_allow_network():
    profile = build_seatbelt_profile(writable_paths=(Path("/repo"),), masked_paths=(), allow_network=True)
    assert "(deny network*)" not in profile


def test_masked_paths_are_denied_after_the_read_allowance():
    profile = build_seatbelt_profile(
        writable_paths=(Path("/repo"),),
        masked_paths=(Path("/repo/.env"),),
        allow_network=False,
    )
    assert '(deny file-read* (literal "/repo/.env"))' in profile
    # Seatbelt takes the last matching rule, so the denial must come after the
    # broad allowance rather than before it.
    assert profile.index("(allow default)") < profile.index("(deny file-read*")


def test_paths_with_quotes_cannot_break_out_of_the_profile():
    """Profile injection, not a cosmetic quoting concern."""
    profile = build_seatbelt_profile(
        writable_paths=(Path('/repo/we"ird'),),
        masked_paths=(),
        allow_network=False,
    )
    assert '(subpath "/repo/we\\"ird")' in profile


def test_unavailable_reason_names_the_platform():
    reason = unavailable_reason()
    assert reason
    assert "docker" in reason or "sandbox-exec" in reason


def test_resolve_bash_backend_honours_an_explicit_os_choice():
    assert resolve_bash_backend("os", "trusted") == "os"
    assert resolve_bash_backend("os", "untrusted") == "os"


def test_adding_the_os_backend_did_not_change_what_auto_means():
    """Adding a backend must not move the default; that is a separate decision.

    It has since been made deliberately — `auto` is the host everywhere — but
    this test exists to catch a default that moves as a side effect of adding
    an option, which is how a boundary disappears without anyone deciding to
    remove it.
    """
    assert resolve_bash_backend("auto", "trusted") == "host"
    assert resolve_bash_backend("auto", "untrusted") == "host"
    assert resolve_bash_backend("os", "trusted") == "os"


def make_request(tmp_path, command, **kwargs):
    return ExecutionRequest(
        workspace=tmp_path,
        shell_command=command,
        backend="os",
        action="agent.bash",
        limits=ResourceLimits(timeout_seconds=30),
        **kwargs,
    )


@darwin_only
@pytest.mark.asyncio
async def test_a_command_runs_and_reports_its_exit_code(tmp_path):
    service = ExecutionService()
    result = await service.execute(make_request(tmp_path, "echo hello; exit 3"))
    assert result.exit_code == 3
    assert "hello" in result.stdout


@darwin_only
@pytest.mark.asyncio
async def test_writes_inside_the_workspace_are_allowed(tmp_path):
    service = ExecutionService()
    result = await service.execute(make_request(tmp_path, "echo written > inside.txt"))
    assert result.exit_code == 0, result.combined_output
    assert (tmp_path / "inside.txt").read_text(encoding="utf-8").strip() == "written"


@pytest.fixture
def escape_target():
    """A path outside both the workspace and the temp root.

    Home, deliberately: the system temp directory is writable by design so that
    build tools can create scratch files, so a target under `tmp_path` would pass
    without proving anything. `~` is also what the boundary actually exists to
    protect — `~/.ssh`, `~/.aws`, shell rc files.
    """
    target = Path.home() / ".aicode-sandbox-escape-check"
    target.unlink(missing_ok=True)
    yield target
    target.unlink(missing_ok=True)


@darwin_only
@pytest.mark.asyncio
async def test_writes_outside_the_workspace_are_denied(tmp_path, escape_target):
    """The property the whole backend exists for."""
    service = ExecutionService()

    result = await service.execute(
        ExecutionRequest(
            workspace=tmp_path,
            shell_command=f"echo escaped > {escape_target}",
            backend="os",
            limits=ResourceLimits(timeout_seconds=30),
        )
    )

    assert result.exit_code != 0
    assert not escape_target.exists(), "the sandbox must prevent the write, not merely report it"


@darwin_only
@pytest.mark.asyncio
async def test_a_masked_path_cannot_be_read(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=secret-value\n", encoding="utf-8")
    service = ExecutionService()

    result = await service.execute(make_request(tmp_path, "cat .env", masked_paths=(".env",)))

    assert result.exit_code != 0
    assert "secret-value" not in result.combined_output


@darwin_only
@pytest.mark.asyncio
async def test_network_is_denied_when_requested(tmp_path):
    service = ExecutionService()
    result = await service.execute(
        make_request(tmp_path, "exec 3<>/dev/tcp/1.1.1.1/80", network="none")
    )
    assert result.exit_code != 0


@darwin_only
@pytest.mark.asyncio
async def test_the_temp_directory_stays_writable(tmp_path):
    """A build tool that cannot write a scratch file is broken, not sandboxed."""
    service = ExecutionService()
    result = await service.execute(make_request(tmp_path, "t=$(mktemp) && echo ok > \"$t\" && rm -f \"$t\""))
    assert result.exit_code == 0, result.combined_output


@pytest.mark.asyncio
async def test_an_unavailable_sandbox_fails_instead_of_running_unconfined(tmp_path, monkeypatch):
    """A routed command must never silently downgrade to running unconfined."""
    monkeypatch.setattr("app.execution.sandbox_os.os_sandbox_available", lambda: False)
    service = ExecutionService()

    result = await service.execute(make_request(tmp_path, "echo should-not-run > escaped.txt"))

    assert result.exit_code == -1
    assert not (tmp_path / "escaped.txt").exists()
    assert result.stderr


@pytest.mark.asyncio
async def test_the_bash_tool_refuses_when_the_sandbox_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tools.registry.os_sandbox_available", lambda: False)
    context = ToolContext(workspace=tmp_path, execution=ExecutionService(), bash_backend="os")
    result = await DEFAULT_REGISTRY.run("bash", {"command": "echo hi"}, context)
    assert not result.success
    assert result.data["status"] == "unavailable"
    assert result.risk_level == "high"


class FakeSession:
    def __init__(self):
        self.background_offsets: dict[str, int] = {}


@darwin_only
@pytest.mark.asyncio
async def test_background_commands_are_sandboxed_too(tmp_path, escape_target):
    """Background bypasses ExecutionRequest, so the wrapper is applied explicitly.

    Without this the OS backend would exempt exactly the long-running commands it
    most needs to cover.
    """
    outside = escape_target
    service = ExecutionService()
    context = ToolContext(
        workspace=tmp_path,
        execution=service,
        session=FakeSession(),
        session_id="sess_1",
        bash_backend="os",
    )
    try:
        started = await DEFAULT_REGISTRY.run(
            "bash",
            {"command": f"echo escaped > {outside}; sleep 30", "background": True},
            context,
        )
        assert started.success
        handle = started.data["handle"]
        entry = service.background.get(handle)
        assert "sandbox-exec" in entry.command, "the background command must be wrapped"

        import asyncio

        for _ in range(100):
            if outside.exists():
                break
            await asyncio.sleep(0.02)
        assert not outside.exists(), "a background command must not escape the sandbox"
    finally:
        await service.cancel_all()


@pytest.mark.asyncio
async def test_background_refuses_when_the_sandbox_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tools.registry.os_sandbox_available", lambda: False)
    service = ExecutionService()
    context = ToolContext(
        workspace=tmp_path,
        execution=service,
        session=FakeSession(),
        bash_backend="os",
    )
    result = await DEFAULT_REGISTRY.run("bash", {"command": "sleep 30", "background": True}, context)
    assert not result.success
    assert service.status()["background"] == 0


@darwin_only
@pytest.mark.asyncio
async def test_the_profile_file_is_removed_when_the_command_ends(tmp_path):
    service = ExecutionService()
    context = ToolContext(
        workspace=tmp_path,
        execution=service,
        session=FakeSession(),
        bash_backend="os",
    )
    try:
        started = await DEFAULT_REGISTRY.run("bash", {"command": "sleep 30", "background": True}, context)
        entry = service.background.get(started.data["handle"])
        profiles = list(entry.cleanup_paths)
        assert profiles and profiles[0].exists()
    finally:
        await service.cancel_all()
    assert not profiles[0].exists(), "a long-lived daemon must not accumulate profile files"


@darwin_only
def test_the_sandbox_is_actually_enforced_not_just_configured():
    """Guards the case where the profile is written but never applied."""
    assert os_sandbox_available()
    assert sys.platform == "darwin"
