"""Collect files a sandboxed command wrote to its artifact drop.

The drop is a host directory bind-mounted into the container, so everything in
it was written by a model-chosen command running as the invoking user. Reading
it back is therefore an untrusted-input problem, not a file-copy problem:

- a symlink in the drop can point anywhere the user can read, so following one
  would turn "collect the build output" into an arbitrary-file read;
- the number and size of files is whatever the command decided, so an
  uncapped walk is a disk and memory amplifier driven by the sandbox it is
  supposed to contain.

Both are handled by refusing rather than by sanitising. A skipped entry is
counted, not silently dropped.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.execution.models import ArtifactInfo

# Caps chosen to hold a normal test report, coverage file and build log while
# staying far below anything that would strain the audit log or the response.
MAX_ARTIFACTS = 64
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
_HASH_CHUNK = 1024 * 1024


def collect_artifacts(root: Path) -> tuple[tuple[ArtifactInfo, ...], bool]:
    """Return metadata for regular files under `root`, and whether a cap was hit.

    Ordering is deterministic so two runs of the same command produce the same
    audit record.
    """
    if not root.is_dir():
        return (), False

    resolved_root = root.resolve()
    collected: list[ArtifactInfo] = []
    truncated = False
    total_bytes = 0

    for path in sorted(root.rglob("*")):
        if len(collected) >= MAX_ARTIFACTS:
            truncated = True
            break
        # `is_file()` follows symlinks, so the symlink check has to come first.
        if path.is_symlink():
            truncated = True
            continue
        if not path.is_file():
            continue
        # Belt and braces: a path that does not resolve back inside the drop is
        # not ours to read, however it got there.
        try:
            path.resolve().relative_to(resolved_root)
        except ValueError:
            truncated = True
            continue
        size = path.stat().st_size
        if size > MAX_ARTIFACT_BYTES or total_bytes + size > MAX_TOTAL_BYTES:
            truncated = True
            continue
        total_bytes += size
        collected.append(
            ArtifactInfo(
                path=path.relative_to(root).as_posix(),
                size_bytes=size,
                sha256=_file_digest(path),
            )
        )

    return tuple(collected), truncated


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
