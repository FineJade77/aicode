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
