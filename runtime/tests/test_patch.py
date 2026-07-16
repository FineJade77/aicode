from pathlib import Path

import pytest

from app.tools.base import ToolError
from app.tools.patch import apply_content_patch, create_append_patch, create_file_patch, create_replace_patch


def test_create_append_patch_and_apply(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("hello\n", encoding="utf-8")

    proposal = create_append_patch(tmp_path, "README.md", "world")

    assert proposal.path == "README.md"
    assert "+world" in proposal.diff

    apply_content_patch(tmp_path, proposal.path, proposal.new_content)

    assert readme.read_text(encoding="utf-8") == "hello\nworld\n"


def test_create_replace_patch_and_apply(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("hello\nold value\n", encoding="utf-8")

    proposal = create_replace_patch(tmp_path, "README.md", "old value", "new value")

    assert proposal.path == "README.md"
    assert "-old value" in proposal.diff
    assert "+new value" in proposal.diff

    apply_content_patch(tmp_path, proposal.path, proposal.new_content)

    assert readme.read_text(encoding="utf-8") == "hello\nnew value\n"


def test_create_replace_patch_requires_unique_old_text(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("same\nsame\n", encoding="utf-8")

    with pytest.raises(ToolError, match="出现 2 次"):
        create_replace_patch(tmp_path, "README.md", "same", "new")


def test_create_file_patch_and_apply(tmp_path: Path) -> None:
    proposal = create_file_patch(tmp_path, "NEW.md", "hello")

    assert proposal.path == "NEW.md"
    assert proposal.diff.startswith("--- /dev/null")
    assert "+hello" in proposal.diff

    apply_content_patch(tmp_path, proposal.path, proposal.new_content, allow_create=True)

    assert (tmp_path / "NEW.md").read_text(encoding="utf-8") == "hello\n"


def test_create_file_patch_rejects_existing_file(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("exists\n", encoding="utf-8")

    with pytest.raises(ToolError, match="文件已存在"):
        create_file_patch(tmp_path, "README.md", "new")


def test_create_file_patch_requires_existing_parent(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="父目录不存在"):
        create_file_patch(tmp_path, "missing/NEW.md", "hello")
