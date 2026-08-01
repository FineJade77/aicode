"""OS-level sandboxing, as an alternative to the container backend.

The Docker backend is a real boundary but an expensive one: it needs a daemon, a
pre-pulled image, and hundreds of milliseconds per command. That cost is why
trusted workspaces run bare today — the only sandbox on offer was too heavy to
apply by default.

An OS-level sandbox starts in milliseconds and needs nothing installed. It is a
*weaker* boundary than a container — same filesystem namespace, same kernel, no
resource limits — so it is offered as its own backend rather than as a silent
substitute for Docker.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from app.execution.host import HostExecutionBackend
from app.execution.models import ExecutionRequest, ExecutionResult, ExecutionStatus

SEATBELT_BINARY = "sandbox-exec"

# Writable regardless of workspace: a shell that cannot open /dev/null or its own
# file descriptors fails in ways that look like the command being broken.
ALWAYS_WRITABLE_LITERALS = (
    "/dev/null",
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/tty",
    "/dev/dtracehelper",
)
ALWAYS_WRITABLE_SUBPATHS = ("/dev/fd",)


def os_sandbox_available() -> bool:
    """Whether this platform can enforce an OS-level sandbox.

    Callers check before dispatching, so a command routed to the sandbox fails
    loudly instead of running unconfined. Anything other than macOS returns False
    today: Linux would need landlock or bubblewrap, and claiming a boundary that
    is not enforced is worse than declaring the platform unsupported.
    """
    return sys.platform == "darwin" and bool(shutil.which(SEATBELT_BINARY))


def unavailable_reason() -> str:
    if sys.platform != "darwin":
        return (
            f"the OS-level sandbox is currently implemented with macOS seatbelt and this host is "
            f"{sys.platform!r}; use execution.agentBashBackend \"docker\" or \"host\""
        )
    return f"{SEATBELT_BINARY} was not found on PATH, so the OS-level sandbox cannot be applied"


def build_seatbelt_profile(
    *,
    writable_paths: tuple[Path, ...],
    masked_paths: tuple[Path, ...],
    allow_network: bool,
) -> str:
    """Build a seatbelt profile that denies writes outside the workspace.

    Deliberately `(allow default)` plus targeted denials rather than
    `(deny default)` plus an allowlist. A deny-by-default profile has to
    enumerate every mach service, sysctl and shared-memory region a real
    toolchain touches; getting that list wrong does not weaken the sandbox, it
    breaks compilers in ways that read as mysterious tool failures. Denying
    writes and network outright is what this boundary is actually for, and it is
    enforceable without guessing at that list.
    """
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write*",
    ]
    for path in writable_paths:
        lines.append(f'  (subpath {_sbpl_string(str(path))})')
    for literal in ALWAYS_WRITABLE_LITERALS:
        lines.append(f"  (literal {_sbpl_string(literal)})")
    for subpath in ALWAYS_WRITABLE_SUBPATHS:
        lines.append(f"  (subpath {_sbpl_string(subpath)})")
    lines.append(")")
    for path in masked_paths:
        # Masked paths are denied *after* the broad read allowance, which is the
        # order seatbelt evaluates: last matching rule wins.
        lines.append(f"(deny file-read* (subpath {_sbpl_string(str(path))}))")
        lines.append(f"(deny file-read* (literal {_sbpl_string(str(path))}))")
    if not allow_network:
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


def _sbpl_string(value: str) -> str:
    """Quote a path for a seatbelt profile.

    A path containing a quote or backslash would otherwise terminate the string
    early and change the meaning of the rule — a profile-injection bug, not a
    cosmetic one.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class OsSandboxExecutionBackend:
    """Runs commands under the platform sandbox, delegating process handling.

    Wraps the host backend rather than reimplementing it: process groups,
    timeouts, cancellation and output draining are already solved there, and the
    only difference here is the argv the shell is launched through.
    """

    def __init__(self, host: HostExecutionBackend) -> None:
        self.host = host

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if not request.shell_command:
            raise ValueError("OS sandbox backend requires shell_command")
        if not os_sandbox_available():
            return ExecutionResult(
                execution_id=request.execution_id,
                backend=request.backend,
                status=ExecutionStatus.FAILED,
                exit_code=-1,
                stderr=unavailable_reason(),
            )

        workspace = request.workspace.expanduser().resolve()
        writable = _writable_paths(request, workspace)
        masked = tuple(_resolve_masked(workspace, request.masked_paths))
        profile = build_seatbelt_profile(
            writable_paths=writable,
            masked_paths=masked,
            allow_network=request.network != "none",
        )
        with tempfile.TemporaryDirectory(prefix="aicode-seatbelt-") as temp_dir:
            profile_path = Path(temp_dir) / "profile.sb"
            profile_path.write_text(profile, encoding="utf-8")
            sandboxed = replace(
                request,
                argv=(SEATBELT_BINARY, "-f", str(profile_path), "/bin/sh", "-c", request.shell_command),
                shell_command=None,
            )
            return await self.host.execute(sandboxed)

    async def cancel(self, execution_id: str) -> bool:
        return await self.host.cancel(execution_id)

    async def cancel_all(self) -> None:
        await self.host.cancel_all()


def _writable_paths(request: ExecutionRequest, workspace: Path) -> tuple[Path, ...]:
    paths = [workspace]
    for candidate in request.writable_paths:
        resolved = candidate.expanduser().resolve()
        if resolved not in paths:
            paths.append(resolved)
    # The system temp directory: build tools that cannot write a scratch file are
    # not usefully sandboxed, they are just broken.
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root not in paths:
        paths.append(temp_root)
    return tuple(paths)


def _resolve_masked(workspace: Path, masked: tuple[str, ...]) -> list[Path]:
    resolved: list[Path] = []
    for entry in masked:
        candidate = Path(entry)
        target = candidate if candidate.is_absolute() else workspace / candidate
        # Not `.resolve()`: a masked path that does not exist yet still has to be
        # denied, and resolve() on a dangling symlink would hand back a path the
        # rule no longer covers.
        absolute = Path(str(target))
        if absolute not in resolved:
            resolved.append(absolute)
    return resolved
