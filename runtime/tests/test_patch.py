from pathlib import Path

from app.tools.patch import apply_content_patch, create_append_patch


def test_create_append_patch_and_apply(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("hello\n", encoding="utf-8")

    proposal = create_append_patch(tmp_path, "README.md", "world")

    assert proposal.path == "README.md"
    assert "+world" in proposal.diff

    apply_content_patch(tmp_path, proposal.path, proposal.new_content)

    assert readme.read_text(encoding="utf-8") == "hello\nworld\n"
