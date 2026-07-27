from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


TRUST_SCHEMA_VERSION = 1


class TrustStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def from_env(cls) -> "TrustStore":
        explicit = os.getenv("AICODE_TRUST_PATH")
        if explicit:
            return cls(Path(explicit).expanduser())
        home = os.getenv("AICODE_HOME")
        base = Path(home).expanduser() if home else Path.home() / ".aicode"
        return cls(base / "trust.json")

    def status(self, workspace: Path) -> dict[str, Any]:
        canonical = canonical_workspace(workspace)
        self._ensure_external_to(canonical)
        remote = git_remote(canonical)
        entry = self._read()["projects"].get(workspace_key(canonical))
        if not isinstance(entry, dict) or entry.get("workspace") != str(canonical):
            return trust_status(canonical, remote, "untrusted", "not_recorded")
        recorded_remote = sanitize_git_remote(str(entry.get("git_remote") or ""))
        if recorded_remote and recorded_remote != remote:
            return trust_status(canonical, remote, "untrusted", "git_remote_changed", recorded_remote)
        return trust_status(canonical, remote, str(entry.get("level") or "untrusted"), "recorded", recorded_remote)

    def trust(self, workspace: Path) -> dict[str, Any]:
        canonical = canonical_workspace(workspace)
        self._ensure_external_to(canonical)
        remote = git_remote(canonical)
        data = self._read()
        data["projects"][workspace_key(canonical)] = {
            "workspace": str(canonical),
            "level": "trusted",
            "git_remote": remote,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(data)
        return self.status(canonical)

    def remove(self, workspace: Path) -> bool:
        canonical = canonical_workspace(workspace)
        self._ensure_external_to(canonical)
        data = self._read()
        removed = data["projects"].pop(workspace_key(canonical), None) is not None
        if removed:
            self._write(data)
        return removed

    def list(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for entry in self._read()["projects"].values():
            if not isinstance(entry, dict):
                continue
            workspace_text = str(entry.get("workspace") or "")
            if not workspace_text:
                continue
            workspace = Path(workspace_text)
            if not workspace.is_dir():
                status = trust_status(
                    workspace,
                    "",
                    "untrusted",
                    "workspace_missing",
                    sanitize_git_remote(str(entry.get("git_remote") or "")),
                )
            else:
                status = self.status(workspace)
            status["updated_at"] = str(entry.get("updated_at") or "")
            entries.append(status)
        return sorted(entries, key=lambda item: str(item.get("workspace") or ""))

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": TRUST_SCHEMA_VERSION, "projects": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"failed to read the project trust store: {exc}") from exc
        if raw.get("schema_version") != TRUST_SCHEMA_VERSION or not isinstance(raw.get("projects"), dict):
            raise ValueError("project trust store schema is invalid")
        return raw

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        descriptor, temp_name = tempfile.mkstemp(prefix=".trust-", dir=self.path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _ensure_external_to(self, workspace: Path) -> None:
        path = self.path.expanduser().resolve(strict=False)
        try:
            path.relative_to(workspace)
        except ValueError:
            return
        raise ValueError("project trust store must be outside the workspace")


def canonical_workspace(workspace: Path) -> Path:
    canonical = workspace.expanduser().resolve()
    if not canonical.is_dir():
        raise ValueError(f"workspace does not exist or is not a directory: {canonical}")
    return canonical


def workspace_key(workspace: Path) -> str:
    return hashlib.sha256(str(workspace).encode()).hexdigest()


def trust_status(
    workspace: Path,
    current_remote: str,
    level: str,
    reason: str,
    recorded_remote: str = "",
) -> dict[str, Any]:
    return {
        "workspace": str(workspace),
        "level": "trusted" if level == "trusted" else "untrusted",
        "git_remote": current_remote,
        "recorded_remote": recorded_remote,
        "reason": reason,
    }


def git_remote(workspace: Path) -> str:
    config_path = git_config_path(workspace)
    if config_path is None:
        return ""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return ""
    preferred = 'remote "origin"'
    if parser.has_option(preferred, "url"):
        return sanitize_git_remote(parser.get(preferred, "url").strip())
    for section in parser.sections():
        if section.startswith('remote "') and parser.has_option(section, "url"):
            return sanitize_git_remote(parser.get(section, "url").strip())
    return ""


def sanitize_git_remote(remote: str) -> str:
    if "://" not in remote:
        scp_like = re.fullmatch(r"(?:(git)@|[^@/]+@)?([^:/]+):(.+)", remote)
        if scp_like:
            prefix = "git@" if scp_like.group(1) else ""
            return f"{prefix}{scp_like.group(2)}:{scp_like.group(3)}"
        return remote
    try:
        parsed = urlsplit(remote)
        if not parsed.hostname:
            return remote
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
    except ValueError:
        return ""
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def git_config_path(workspace: Path) -> Path | None:
    dot_git = workspace / ".git"
    if dot_git.is_dir():
        return safe_git_config(dot_git)
    if not dot_git.is_file():
        return None
    try:
        marker = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not marker.startswith("gitdir:"):
        return None
    git_dir = Path(marker.removeprefix("gitdir:").strip())
    if not git_dir.is_absolute():
        git_dir = workspace / git_dir
    git_dir = git_dir.resolve()
    if not (git_dir / "HEAD").is_file():
        return None
    direct = safe_git_config(git_dir)
    if direct is not None:
        return direct
    common_marker = git_dir / "commondir"
    try:
        common_dir = Path(common_marker.read_text(encoding="utf-8").strip())
    except OSError:
        return None
    if not common_dir.is_absolute():
        common_dir = git_dir / common_dir
    return safe_git_config(common_dir.resolve())


def safe_git_config(git_dir: Path) -> Path | None:
    candidate = git_dir / "config"
    try:
        resolved_dir = git_dir.resolve()
        resolved = candidate.resolve()
        resolved.relative_to(resolved_dir)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None
