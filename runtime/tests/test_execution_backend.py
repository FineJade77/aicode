from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.audit.logger import AuditLogger
from app.execution.docker import DockerExecutionBackend, missing_image_hint
from app.execution.host import build_subprocess_environment, isolated_execution_home
from app.execution.models import ExecutionRequest, ExecutionResult, ExecutionStatus, ResourceLimits
from app.execution.service import ExecutionService


@pytest.mark.asyncio
async def test_host_execution_has_stable_terminal_result_and_safe_audit(tmp_path: Path) -> None:
    audit = AuditLogger(tmp_path / "audit.jsonl")
    service = ExecutionService(audit=audit)
    secret_command = "print('literal-secret-value')"
    request = ExecutionRequest(
        execution_id="exec_safe",
        workspace=tmp_path,
        argv=(sys.executable, "-c", secret_command),
        action="test",
        session_id="sess_1",
        run_id="run_1",
        tool_call_id="tool_1",
        limits=ResourceLimits(timeout_seconds=5),
    )

    result = await service.execute(request)
    await audit.flush()

    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.exit_code == 0
    assert "literal-secret-value" in result.stdout
    events = [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in events] == ["execution.started", "execution.finished"]
    assert events[-1]["data"]["status"] == "succeeded"
    assert events[-1]["data"]["exit_code"] == 0
    audit_text = audit.path.read_text(encoding="utf-8")
    assert secret_command not in audit_text
    assert "command_hash" in audit_text
    await audit.aclose()


@pytest.mark.asyncio
async def test_explicit_cancel_terminates_process_group(tmp_path: Path) -> None:
    service = ExecutionService()
    marker = tmp_path / "orphan"
    child = f"import pathlib, time; time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('leaked')"
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(5)"
    request = ExecutionRequest(
        execution_id="exec_cancel",
        workspace=tmp_path,
        argv=(sys.executable, "-c", parent),
        limits=ResourceLimits(timeout_seconds=10),
    )

    task = asyncio.create_task(service.execute(request))
    await asyncio.sleep(0.1)
    assert await service.cancel("exec_cancel") is True
    result = await asyncio.wait_for(task, timeout=2)

    assert result.status == ExecutionStatus.CANCELLED
    assert result.cancelled is True
    assert service.status()["active"] == 0
    await asyncio.sleep(1)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_docker_backend_builds_read_only_isolated_request(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    host = CapturingHost()
    backend = DockerExecutionBackend(host)  # type: ignore[arg-type]
    request = ExecutionRequest(
        execution_id="exec_docker",
        workspace=tmp_path,
        backend="docker",
        shell_command="go test ./...",
        action="test",
        network="none",
        masked_paths=(".env*",),
        limits=ResourceLimits(timeout_seconds=60, cpus="1.5", memory="768m", pids_limit=128),
    )

    result = await backend.execute(request)

    assert result.status == ExecutionStatus.SUCCEEDED
    assert host.request is not None
    argv = list(host.request.argv or ())
    joined = " ".join(argv)
    assert argv[:3] == ["docker", "run", "--rm"]
    # Implicit image pulls would turn one tool call into a silent long download.
    assert "--pull=never" in argv
    assert "--network none" in joined
    assert "--cpus 1.5" in joined
    assert "--memory 768m" in joined
    assert "--pids-limit 128" in joined
    assert f"type=bind,src={tmp_path.resolve()},dst=/workspace,readonly" in joined
    assert "dst=/workspace/.env,readonly" in joined
    assert argv[-3:] == ["sh", "-lc", "go test ./..."]
    assert "PATH" in (host.request.env_allowlist or ())
    assert "OPENAI_API_KEY" not in (host.request.env_allowlist or ())


@pytest.mark.asyncio
async def test_docker_backend_mounts_workspace_writable_for_agent_commands(tmp_path: Path) -> None:
    """Agent shell commands need to create files and run builds, so the sandbox
    mounts the workspace read-write and drops privileges to the host uid/gid."""
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    host = CapturingHost()
    backend = DockerExecutionBackend(host)  # type: ignore[arg-type]
    request = ExecutionRequest(
        execution_id="exec_docker_agent",
        workspace=tmp_path,
        backend="docker",
        shell_command="mkdir -p build",
        action="agent.bash",
        network="none",
        writable_paths=(tmp_path,),
        masked_paths=(".env*",),
        limits=ResourceLimits(timeout_seconds=60),
    )

    result = await backend.execute(request)

    assert result.status == ExecutionStatus.SUCCEEDED
    assert host.request is not None
    joined = " ".join(host.request.argv or ())
    assert f"type=bind,src={tmp_path.resolve()},dst=/workspace " in joined + " "
    assert f"type=bind,src={tmp_path.resolve()},dst=/workspace,readonly" not in joined
    assert f"--user {os.getuid()}:{os.getgid()}" in joined
    # Network isolation and .env masking must survive the writable mount.
    assert "--network none" in joined
    assert "dst=/workspace/.env,readonly" in joined


@pytest.mark.asyncio
async def test_docker_backend_rejects_writable_paths_outside_workspace(tmp_path: Path) -> None:
    """Only the workspace may be writable; any other host path would put an
    unvetted directory inside a container running model-chosen commands."""
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = DockerExecutionBackend(CapturingHost())  # type: ignore[arg-type]
    request = ExecutionRequest(
        execution_id="exec_docker_escape",
        workspace=workspace,
        backend="docker",
        shell_command="ls",
        network="none",
        writable_paths=(outside,),
    )

    with pytest.raises(ValueError, match="writable path"):
        await backend.execute(request)


class CapturingHost:
    def __init__(self) -> None:
        self.request: ExecutionRequest | None = None

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.request = request
        return ExecutionResult(
            execution_id=request.execution_id,
            backend=request.backend,
            status=ExecutionStatus.SUCCEEDED,
            exit_code=0,
        )

    async def cancel(self, execution_id: str) -> bool:
        return False

    async def cancel_all(self) -> None:
        return None


@pytest.mark.asyncio
async def test_docker_backend_integration_when_local_image_is_available(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI unavailable")
    probe = subprocess.run(
        ["docker", "image", "inspect", "ubuntu:24.04"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("ubuntu:24.04 image or Docker daemon unavailable")

    service = ExecutionService()
    result = await service.execute(
        ExecutionRequest(
            execution_id="exec_docker_integration",
            workspace=tmp_path,
            backend="docker",
            shell_command="printf execution-ok",
            network="none",
            image="ubuntu:24.04",
            limits=ResourceLimits(timeout_seconds=30, cpus="1", memory="128m", pids_limit=32),
        )
    )

    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.stdout == "execution-ok"


@pytest.mark.asyncio
async def test_host_environment_allowlist_excludes_provider_and_runtime_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AICODE_HOME", str(tmp_path / "aicode-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("AICODE_RUNTIME_TOKEN", "runtime-secret")
    monkeypatch.setenv("SAFE_CUSTOM_VALUE", "should-not-pass")
    script = (
        "import json, os; print(json.dumps({key: os.getenv(key) for key in "
        "['OPENAI_API_KEY','ANTHROPIC_API_KEY','AICODE_RUNTIME_TOKEN','SAFE_CUSTOM_VALUE','HOME','PATH','TMPDIR']}))"
    )

    result = await ExecutionService().execute(
        ExecutionRequest(
            workspace=tmp_path,
            argv=(sys.executable, "-c", script),
            limits=ResourceLimits(timeout_seconds=5),
        )
    )
    environment = json.loads(result.stdout)

    assert environment["OPENAI_API_KEY"] is None
    assert environment["ANTHROPIC_API_KEY"] is None
    assert environment["AICODE_RUNTIME_TOKEN"] is None
    assert environment["SAFE_CUSTOM_VALUE"] is None
    assert environment["HOME"] == str(isolated_execution_home(tmp_path))
    assert environment["PATH"]
    assert environment["TMPDIR"] == str(isolated_execution_home(tmp_path) / "tmp")


def test_explicit_environment_allowlist_cannot_request_sensitive_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    environment = build_subprocess_environment(("OPENAI_API_KEY", "PATH"))

    assert environment == {"PATH": "/usr/bin:/bin"}


def test_isolated_execution_home_is_private_and_scoped_per_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AICODE_HOME", str(tmp_path / "state"))
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_home = isolated_execution_home(first)
    second_home = isolated_execution_home(second)

    assert first_home != second_home
    assert first_home.parent == second_home.parent
    assert first_home.stat().st_mode & 0o777 == 0o700
    assert second_home.stat().st_mode & 0o777 == 0o700


def test_missing_image_hint_maps_docker_error_to_action() -> None:
    hint = missing_image_hint("pytest -q", "Unable to find image 'python:3.12-slim' locally")
    assert "docker pull python:3.12-slim" in hint


def test_missing_image_hint_ignores_unrelated_failures() -> None:
    assert missing_image_hint("ls", "ls: cannot access 'x': No such file or directory") == ""


# --- the sandbox artifact drop ------------------------------------------------
#
# The drop is a host directory that model-chosen commands write to, so reading it
# back is an untrusted-input problem. These pin the two refusals that make it
# safe, and the mount shape that keeps the workspace read-only while it exists.

from app.execution.artifacts import (  # noqa: E402
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS,
    collect_artifacts,
)
from app.execution.docker import ARTIFACT_MOUNT, _docker_args  # noqa: E402


def test_the_drop_is_writable_while_the_workspace_stays_read_only(tmp_path: Path) -> None:
    """The whole point of a separate drop: output without a writable repository."""
    drop = tmp_path / "drop"
    drop.mkdir()
    request = ExecutionRequest(
        workspace=tmp_path,
        shell_command="make build",
        backend="docker",
        network="none",
        artifact_root=drop,
    )

    args = _docker_args(request, tmp_path, [])
    rendered = " ".join(args)

    assert f"type=bind,src={tmp_path},dst=/workspace,readonly" in rendered
    assert f"type=bind,src={drop},dst={ARTIFACT_MOUNT}" in rendered
    assert f"dst={ARTIFACT_MOUNT},readonly" not in rendered
    # A command has to be able to find the drop without being told where it is.
    assert f"AICODE_ARTIFACTS={ARTIFACT_MOUNT}" in rendered
    # Files aicode cannot read afterwards are not an export.
    assert "--user" in args


def test_without_artifacts_nothing_is_mounted_or_advertised(tmp_path: Path) -> None:
    request = ExecutionRequest(
        workspace=tmp_path, shell_command="make test", backend="docker", network="none"
    )

    rendered = " ".join(_docker_args(request, tmp_path, []))

    assert ARTIFACT_MOUNT not in rendered
    assert "AICODE_ARTIFACTS" not in rendered


def test_collecting_records_metadata_but_not_contents(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "junit.xml").write_text("<testsuite/>", encoding="utf-8")

    artifacts, truncated = collect_artifacts(tmp_path)

    assert truncated is False
    assert [artifact.path for artifact in artifacts] == ["reports/junit.xml"]
    assert artifacts[0].size_bytes == len("<testsuite/>")
    assert len(artifacts[0].sha256) == 64
    # Nothing on the record should carry the bytes themselves.
    assert not hasattr(artifacts[0], "content")


def test_a_symlink_in_the_drop_is_refused_not_followed(tmp_path: Path) -> None:
    """Otherwise "collect the build output" becomes an arbitrary-file read.

    The container runs as the invoking user, so a symlink planted in the drop
    resolves against everything that user can read.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "innocent.log").symlink_to(secret)

    artifacts, truncated = collect_artifacts(drop)

    assert artifacts == ()
    assert truncated is True


def test_a_file_too_large_is_skipped_and_declared(tmp_path: Path) -> None:
    (tmp_path / "huge.bin").write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
    (tmp_path / "small.txt").write_text("ok", encoding="utf-8")

    artifacts, truncated = collect_artifacts(tmp_path)

    assert [artifact.path for artifact in artifacts] == ["small.txt"]
    assert truncated is True


def test_too_many_files_stop_at_the_cap(tmp_path: Path) -> None:
    for index in range(MAX_ARTIFACTS + 5):
        (tmp_path / f"file-{index:03d}.txt").write_text("x", encoding="utf-8")

    artifacts, truncated = collect_artifacts(tmp_path)

    assert len(artifacts) == MAX_ARTIFACTS
    assert truncated is True


def test_collection_is_ordered_so_two_runs_agree(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")

    first, _ = collect_artifacts(tmp_path)
    second, _ = collect_artifacts(tmp_path)

    assert [artifact.path for artifact in first] == ["a.txt", "b.txt"]
    assert first == second


def test_an_absent_drop_collects_nothing(tmp_path: Path) -> None:
    assert collect_artifacts(tmp_path / "missing") == ((), False)
