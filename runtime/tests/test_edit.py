import pytest

from app.tools.edit import EditError, EditStaleError, apply_edit, build_edit_proposal


def test_replace_generates_diff_and_applies(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    proposal = build_edit_proposal(tmp_path, {"path": "a.py", "old_text": "return 1", "new_text": "return 2"}, [])
    assert proposal.kind == "replace"
    assert "-    return 1" in proposal.diff
    assert "+    return 2" in proposal.diff
    apply_edit(tmp_path, proposal)
    assert target.read_text(encoding="utf-8") == "def f():\n    return 2\n"


def test_replace_requires_unique_match(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    with pytest.raises(EditError, match="唯一"):
        build_edit_proposal(tmp_path, {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, [])


def test_replace_missing_old_text(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(EditError, match="未找到"):
        build_edit_proposal(tmp_path, {"path": "a.py", "old_text": "not there", "new_text": "y"}, [])


def test_create_new_file(tmp_path):
    proposal = build_edit_proposal(tmp_path, {"path": "new/b.py", "new_text": "print(1)\n"}, [])
    assert proposal.kind == "create"
    apply_edit(tmp_path, proposal)
    assert (tmp_path / "new" / "b.py").read_text(encoding="utf-8") == "print(1)\n"


def test_create_existing_file_rejected(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    with pytest.raises(EditError, match="old_text"):
        build_edit_proposal(tmp_path, {"path": "a.py", "new_text": "y"}, [])


def test_delete_file(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    proposal = build_edit_proposal(tmp_path, {"path": "a.py", "delete": True}, [])
    assert proposal.kind == "delete"
    apply_edit(tmp_path, proposal)
    assert not target.exists()


def test_stale_detection(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    proposal = build_edit_proposal(tmp_path, {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, [])
    target.write_text("x = 999\n", encoding="utf-8")  # 外部修改
    with pytest.raises(EditStaleError):
        apply_edit(tmp_path, proposal)


def test_protected_path_rejected(tmp_path):
    with pytest.raises(EditError):
        build_edit_proposal(tmp_path, {"path": ".env", "new_text": "SECRET=1"}, [".env"])


def test_stale_detection_create_collision(tmp_path):
    proposal = build_edit_proposal(tmp_path, {"path": "new/b.py", "new_text": "print(1)\n"}, [])
    target = tmp_path / "new" / "b.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("externally created\n", encoding="utf-8")  # 外部并发创建
    with pytest.raises(EditStaleError):
        apply_edit(tmp_path, proposal)
    assert target.read_text(encoding="utf-8") == "externally created\n"


def test_stale_detection_external_delete(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    proposal = build_edit_proposal(tmp_path, {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, [])
    target.unlink()  # 外部删除
    with pytest.raises(EditStaleError):
        apply_edit(tmp_path, proposal)
    assert not target.exists()


def test_non_utf8_file_rejected_without_corruption(tmp_path):
    target = tmp_path / "a.bin"
    original_bytes = b"x = 1\n\xff\xfegarbage\ny = 2\n"
    target.write_bytes(original_bytes)
    with pytest.raises(EditError, match="UTF-8"):
        build_edit_proposal(tmp_path, {"path": "a.bin", "old_text": "y = 2", "new_text": "y = 3"}, [])
    assert target.read_bytes() == original_bytes
