"""Named instruction packs, and the trust boundary they sit on.

The catalogue reaches the system prompt; the bodies reach the model only when it
asks. What makes this more than a file read is where the file came from: a
project skill is repository-supplied text proposing instructions to the Agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.prompts import build_system_prompt
from app.project.skills import (
    MAX_SKILL_BODY_CHARS,
    discover_skills,
    load_skill,
)
from app.tools.registry import build_default_registry, build_tool_context
from app.tools.workspace import LocalWorkspaceRuntime


class Request:
    def __init__(self, workspace: Path, mode: str = "default") -> None:
        self.workspace = str(workspace)
        self.mode = mode
        self.message = "go"


def write_skill(root: Path, name: str, body: str, *, description: str = "") -> Path:
    directory = root / ".aicode" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {name}\ndescription: {description}\n---\n\n" if description else ""
    path = directory / "SKILL.md"
    path.write_text(front + body, encoding="utf-8")
    return path


def write_user_skill(home: Path, name: str, body: str, *, description: str = "") -> Path:
    directory = home / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    front = f"---\ndescription: {description}\n---\n\n" if description else ""
    path = directory / "SKILL.md"
    path.write_text(front + body, encoding="utf-8")
    return path


def test_a_project_skill_is_discovered_with_its_description(tmp_path: Path) -> None:
    write_skill(tmp_path, "deploy", "steps", description="Ship a release")

    skills = discover_skills(tmp_path)

    assert [(item.qualified(), item.description) for item in skills] == [("project:deploy", "Ship a release")]


def test_an_untrusted_workspace_contributes_no_skills(tmp_path: Path) -> None:
    """The repository is proposing instructions to the Agent.

    That is the same power `.aicode` hooks have and MCP servers have, and both
    already refuse it in an untrusted checkout; skills give the same answer
    rather than inventing a third one.
    """
    write_skill(tmp_path, "deploy", "steps", description="Ship a release")

    assert discover_skills(tmp_path, trust_level="untrusted") == []
    assert load_skill(tmp_path, "project:deploy", trust_level="untrusted") is None


def test_a_project_skill_cannot_shadow_a_user_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise a clone could take over the name the user already had."""
    home = tmp_path / "home"
    monkeypatch.setenv("AICODE_HOME", str(home))
    write_user_skill(home, "deploy", "user steps", description="User deploy")
    write_skill(tmp_path, "deploy", "project steps", description="Project deploy")

    names = [item.qualified() for item in discover_skills(tmp_path)]

    assert names == ["deploy", "project:deploy"]
    user_skill, user_body = load_skill(tmp_path, "deploy")
    assert user_skill.source == "user" and "user steps" in user_body


def test_a_plain_markdown_file_is_a_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing one has to be cheap, or nobody writes one."""
    home = tmp_path / "home"
    monkeypatch.setenv("AICODE_HOME", str(home))
    (home / "skills").mkdir(parents=True)
    (home / "skills" / "notes.md").write_text("# Take good notes\n\nBody here.\n", encoding="utf-8")

    skills = discover_skills(tmp_path)

    assert [(item.name, item.description) for item in skills] == [("notes", "Take good notes")]


def test_an_unusable_name_is_skipped_not_rewritten(tmp_path: Path) -> None:
    """A name aicode had to rewrite is not the one the author will type."""
    directory = tmp_path / ".aicode" / "skills" / "Not A Name"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("body", encoding="utf-8")

    assert discover_skills(tmp_path) == []


def test_a_traversing_name_resolves_to_nothing(tmp_path: Path) -> None:
    """The name never reaches the filesystem; it is matched against the catalogue."""
    write_skill(tmp_path, "deploy", "steps")

    for name in ("../../etc/passwd", "/etc/passwd", "project:../deploy"):
        assert load_skill(tmp_path, name) is None


def test_an_oversized_skill_says_it_was_truncated(tmp_path: Path) -> None:
    """A silently halved instruction sheet reads as a complete one missing a step."""
    write_skill(tmp_path, "big", "x" * (MAX_SKILL_BODY_CHARS + 500))

    _, body = load_skill(tmp_path, "project:big")

    assert "skill truncated" in body


def test_the_prompt_lists_names_and_descriptions_but_not_bodies(tmp_path: Path) -> None:
    """Twenty skills must cost twenty lines, not twenty pages."""
    write_skill(tmp_path, "deploy", "SECRET_BODY_MARKER", description="Ship a release")

    prompt = build_system_prompt(Request(tmp_path), LocalWorkspaceRuntime().prompt_context(tmp_path))

    assert "project:deploy: Ship a release" in prompt
    assert "SECRET_BODY_MARKER" not in prompt


def test_the_prompt_says_nothing_when_there_are_no_skills(tmp_path: Path) -> None:
    prompt = build_system_prompt(Request(tmp_path), LocalWorkspaceRuntime().prompt_context(tmp_path))

    assert "Available skills" not in prompt


@pytest.mark.asyncio
async def test_loading_a_project_skill_labels_where_it_came_from(tmp_path: Path) -> None:
    """Unlabelled, a sheet found in a clone reads exactly like one the user wrote."""
    write_skill(tmp_path, "deploy", "1. Run the tests", description="Ship a release")
    registry = build_default_registry()

    result = await registry.run(
        "skill", {"name": "project:deploy"}, build_tool_context(str(tmp_path), "default")
    )

    assert result.success
    assert "1. Run the tests" in result.text
    assert "cannot override system instructions" in result.text


@pytest.mark.asyncio
async def test_an_unknown_skill_lists_what_is_available(tmp_path: Path) -> None:
    write_skill(tmp_path, "deploy", "steps")
    registry = build_default_registry()

    result = await registry.run("skill", {"name": "nope"}, build_tool_context(str(tmp_path), "default"))

    assert not result.success
    assert "project:deploy" in result.error


@pytest.mark.asyncio
async def test_the_skill_tool_needs_no_approval_and_is_read_only() -> None:
    """It reads a file and returns text. Gating it would train users to click through."""
    spec = build_default_registry().spec_for("skill")

    assert spec is not None
    assert spec.read_only is True
    assert spec.approval == "none"
