from __future__ import annotations

from pathlib import Path

import pytest

from app.project.config import mandatory_protected_paths
from app.tools.base import ToolContext
from app.tools.glob import DEFAULT_GLOB_RESULTS, MAX_GLOB_RESULTS
from app.tools.registry import DEFAULT_REGISTRY


def build_tree(root: Path) -> None:
    (root / "src" / "deep").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x\n", encoding="utf-8")
    (root / "src" / "util.py").write_text("x\n", encoding="utf-8")
    (root / "src" / "deep" / "nested.py").write_text("x\n", encoding="utf-8")
    (root / "src" / "notes.md").write_text("x\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("x\n", encoding="utf-8")
    (root / "node_modules" / "pkg" / "index.py").write_text("x\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")


async def run_glob(tmp_path: Path, **args):
    return await DEFAULT_REGISTRY.run(
        "glob",
        args,
        ToolContext(workspace=tmp_path, protected_paths=mandatory_protected_paths()),
    )


@pytest.mark.asyncio
async def test_matches_by_path_pattern(tmp_path: Path) -> None:
    build_tree(tmp_path)

    result = await run_glob(tmp_path, pattern="src/**/*.py")

    assert result.success is True
    assert "src/app.py" in result.text
    assert "src/deep/nested.py" in result.text
    # A path pattern must not pull in files that merely live nearby.
    assert "notes.md" not in result.text
    assert "test_app.py" not in result.text


@pytest.mark.asyncio
async def test_results_are_deterministically_ordered(tmp_path: Path) -> None:
    """Sorted by path rather than mtime: eval runs and tests must not depend on
    filesystem timestamps."""
    build_tree(tmp_path)

    first = await run_glob(tmp_path, pattern="**/*.py")
    second = await run_glob(tmp_path, pattern="**/*.py")

    assert first.text == second.text
    listed = [line for line in first.text.splitlines()[1:]]
    assert listed == sorted(listed)


@pytest.mark.asyncio
async def test_ignored_directories_are_skipped(tmp_path: Path) -> None:
    build_tree(tmp_path)

    result = await run_glob(tmp_path, pattern="**/*.py")

    assert "node_modules" not in result.text


@pytest.mark.asyncio
async def test_protected_paths_never_appear(tmp_path: Path) -> None:
    build_tree(tmp_path)

    result = await run_glob(tmp_path, pattern="*")

    assert ".env" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern",
    ["/etc/*", "../*", "src/../../*", "~/.ssh/*", "**/../../*"],
)
async def test_escaping_patterns_are_rejected(tmp_path: Path, pattern: str) -> None:
    """Checked syntactically before globbing: Path.glob on an absolute or
    parent-relative pattern would happily enumerate outside the tree."""
    build_tree(tmp_path)

    result = await run_glob(tmp_path, pattern=pattern)

    assert result.success is False
    assert result.risk_level == "medium"


@pytest.mark.asyncio
async def test_symlink_escaping_the_workspace_is_not_returned(tmp_path: Path) -> None:
    """A syntactically fine pattern can still reach outside via a symlink."""
    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("x\n", encoding="utf-8")
    (workspace / "link.py").symlink_to(outside / "secret.py")

    result = await DEFAULT_REGISTRY.run("glob", {"pattern": "*.py"}, ToolContext(workspace=workspace))

    assert "link.py" not in result.text


@pytest.mark.asyncio
async def test_result_limit_is_enforced_and_reported(tmp_path: Path) -> None:
    for index in range(30):
        (tmp_path / f"f{index:03d}.py").write_text("x\n", encoding="utf-8")

    result = await run_glob(tmp_path, pattern="*.py", limit=5)

    assert result.data["matches"] == 5
    assert result.data["truncated"] is True
    assert "truncated" in result.text


@pytest.mark.asyncio
async def test_limit_is_clamped_to_the_maximum(tmp_path: Path) -> None:
    build_tree(tmp_path)

    result = await run_glob(tmp_path, pattern="**/*.py", limit=10_000)

    assert result.success is True
    assert result.data["matches"] <= MAX_GLOB_RESULTS


@pytest.mark.asyncio
async def test_no_match_is_a_success_not_an_error(tmp_path: Path) -> None:
    """"Nothing matched" is information, not a failure; reporting it as an error
    would push the model into unnecessary recovery."""
    build_tree(tmp_path)

    result = await run_glob(tmp_path, pattern="**/*.rs")

    assert result.success is True
    assert result.data["matches"] == 0
    assert "no files match" in result.text


@pytest.mark.asyncio
async def test_empty_pattern_is_rejected(tmp_path: Path) -> None:
    assert (await run_glob(tmp_path, pattern="   ")).success is False


@pytest.mark.asyncio
async def test_directories_are_not_returned(tmp_path: Path) -> None:
    build_tree(tmp_path)

    result = await run_glob(tmp_path, pattern="src/*")

    assert "src/deep" not in result.text.replace("src/deep/nested.py", "")


def test_glob_is_read_only_and_needs_no_approval() -> None:
    spec = DEFAULT_REGISTRY.spec_for("glob")
    assert spec is not None
    assert spec.read_only is True
    assert spec.approval == "none"
    assert spec.input_schema["properties"]["limit"]["default"] == DEFAULT_GLOB_RESULTS
