from pathlib import Path

import pytest

from app.tools.base import ToolError
from app.tools.patch import PatchApplication, apply_content_patch, apply_content_patches, create_append_patch, create_file_patch, create_replace_patch


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


def test_apply_content_patches_updates_multiple_files(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("old\n", encoding="utf-8")

    apply_content_patches(
        tmp_path,
        [
            PatchApplication(path="README.md", new_content="new\n"),
            PatchApplication(path="TODO.md", new_content="todo\n", allow_create=True),
        ],
    )

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "TODO.md").read_text(encoding="utf-8") == "todo\n"


def test_apply_content_patches_rolls_back_on_failure(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("old\n", encoding="utf-8")

    with pytest.raises(ToolError, match="父目录不存在"):
        apply_content_patches(
            tmp_path,
            [
                PatchApplication(path="README.md", new_content="new\n"),
                PatchApplication(path="missing/TODO.md", new_content="todo\n", allow_create=True),
            ],
        )

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "old\n"


def test_apply_content_patches_rolls_back_written_files_on_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    readme = tmp_path / "README.md"
    todo = tmp_path / "TODO.md"
    readme.write_text("old\n", encoding="utf-8")
    todo.write_text("todo\n", encoding="utf-8")
    original_write_text = Path.write_text

    def failing_write_text(self: Path, data: str, *args: object, **kwargs: object) -> int:
        if self == todo and data == "new todo\n":
            raise OSError("disk full")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write_text)

    with pytest.raises(ToolError, match="disk full"):
        apply_content_patches(
            tmp_path,
            [
                PatchApplication(path="README.md", new_content="new\n"),
                PatchApplication(path="TODO.md", new_content="new todo\n"),
            ],
        )

    assert readme.read_text(encoding="utf-8") == "old\n"
    assert todo.read_text(encoding="utf-8") == "todo\n"
