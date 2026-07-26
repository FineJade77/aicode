from __future__ import annotations

import json
import os
from pathlib import Path

from app.project.trust import TrustStore, git_remote


def test_trust_defaults_untrusted_and_persists_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store_path = tmp_path / "state" / "trust.json"
    store = TrustStore(store_path)

    assert store.status(workspace)["level"] == "untrusted"
    trusted = store.trust(workspace)

    assert trusted["level"] == "trusted"
    assert store_path.exists()
    assert workspace not in store_path.parents
    assert os.stat(store_path).st_mode & 0o777 == 0o600
    raw = json.loads(store_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert list(raw["projects"].values())[0]["workspace"] == str(workspace.resolve())


def test_trust_binds_git_remote_and_invalidates_when_remote_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    config = git_dir / "config"
    config.write_text('[remote "origin"]\nurl = git@example.test:one/repo.git\n', encoding="utf-8")
    store = TrustStore(tmp_path / "trust.json")

    assert store.trust(workspace)["level"] == "trusted"
    config.write_text('[remote "origin"]\nurl = git@example.test:two/repo.git\n', encoding="utf-8")
    status = store.status(workspace)

    assert status["level"] == "untrusted"
    assert status["reason"] == "git_remote_changed"
    assert status["recorded_remote"] == "git@example.test:one/repo.git"


def test_remove_trust_is_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = TrustStore(tmp_path / "trust.json")
    store.trust(workspace)

    assert store.remove(workspace) is True
    assert store.remove(workspace) is False
    assert store.status(workspace)["level"] == "untrusted"


def test_git_remote_credentials_are_never_stored_or_returned(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / "config").write_text(
        '[remote "origin"]\nurl = https://oauth2:provider-secret@example.test/team/repo.git\n',
        encoding="utf-8",
    )
    store = TrustStore(tmp_path / "trust.json")

    assert git_remote(workspace) == "https://example.test/team/repo.git"
    status = store.trust(workspace)
    persisted = store.path.read_text(encoding="utf-8")

    assert status["git_remote"] == "https://example.test/team/repo.git"
    assert "provider-secret" not in persisted


def test_trust_list_revalidates_missing_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = TrustStore(tmp_path / "trust.json")
    store.trust(workspace)
    workspace.rmdir()

    listed = store.list()

    assert listed[0]["level"] == "untrusted"
    assert listed[0]["reason"] == "workspace_missing"


def test_git_config_symlink_escape_is_not_read(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    outside = tmp_path / "outside-config"
    outside.write_text('[remote "origin"]\nurl = https://token@example.test/repo.git\n', encoding="utf-8")
    (git_dir / "config").symlink_to(outside)

    assert git_remote(workspace) == ""


def test_trust_store_inside_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = TrustStore(workspace / ".aicode" / "trust.json")

    try:
        store.trust(workspace)
    except ValueError as exc:
        assert "workspace 外" in str(exc)
    else:
        raise AssertionError("trust store inside workspace must be rejected")
