from __future__ import annotations

from pathlib import Path

import pytest

from app.sessions.store import SessionStore
from app.tools.base import ToolContext
from app.tools.edit import EditError, apply_edit, build_edit_proposal, file_hash

SOURCE = "alpha = 1\nbeta = 2\ngamma = 3\n"


@pytest.fixture
def workspace(tmp_path: Path):
    (tmp_path / "m.py").write_text(SOURCE, encoding="utf-8")
    session = SessionStore(path=tmp_path / "s.sqlite").create(workspace=str(tmp_path))
    session.record_read("m.py", file_hash(tmp_path / "m.py"))
    return tmp_path, ToolContext(workspace=tmp_path, session=session)


def test_multiple_replacements_apply_together(workspace) -> None:
    tmp_path, context = workspace

    proposal = build_edit_proposal(
        context,
        {
            "path": "m.py",
            "edits": [
                {"old_text": "alpha = 1", "new_text": "alpha = 10"},
                {"old_text": "gamma = 3", "new_text": "gamma = 30"},
            ],
        },
    )
    apply_edit(tmp_path, proposal)

    assert (tmp_path / "m.py").read_text(encoding="utf-8") == "alpha = 10\nbeta = 2\ngamma = 30\n"
    # One proposal means one diff and one approval.
    assert proposal.kind == "replace"
    assert proposal.diff.count("@@") >= 1


def test_replacements_are_matched_against_the_original_not_each_other(workspace) -> None:
    """Sequential str.replace on progressively-updated text would let a later
    old_text match content produced by an earlier replacement — text the model
    never saw — and silently edit the wrong place."""
    tmp_path, context = workspace

    proposal = build_edit_proposal(
        context,
        {
            "path": "m.py",
            "edits": [
                {"old_text": "alpha = 1", "new_text": "beta = 2"},
                {"old_text": "beta = 2", "new_text": "delta = 4"},
            ],
        },
    )
    apply_edit(tmp_path, proposal)

    # The second edit hit the original `beta = 2` line, not the one the first
    # edit just produced.
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == "beta = 2\ndelta = 4\ngamma = 3\n"


def test_any_mismatch_leaves_the_file_untouched(workspace) -> None:
    """A partially applied change is harder to recover from than a rejected one."""
    tmp_path, context = workspace

    with pytest.raises(EditError) as excinfo:
        build_edit_proposal(
            context,
            {
                "path": "m.py",
                "edits": [
                    {"old_text": "alpha = 1", "new_text": "alpha = 10"},
                    {"old_text": "nowhere", "new_text": "x"},
                ],
            },
        )

    assert "edit 2" in str(excinfo.value), "the failure must say which replacement failed"
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == SOURCE


def test_ambiguous_fragment_is_reported_with_its_index(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("x = 1\nx = 1\nz = 3\n", encoding="utf-8")
    session = SessionStore(path=tmp_path / "s.sqlite").create(workspace=str(tmp_path))
    session.record_read("m.py", file_hash(tmp_path / "m.py"))
    context = ToolContext(workspace=tmp_path, session=session)

    with pytest.raises(EditError, match="edit 2"):
        build_edit_proposal(
            context,
            {
                "path": "m.py",
                "edits": [{"old_text": "z = 3", "new_text": "z = 30"}, {"old_text": "x = 1", "new_text": "y"}],
            },
        )


def test_a_single_element_batch_reads_like_the_single_form(tmp_path: Path) -> None:
    """A one-item batch is the single form, so it must not gain an "edit 1:"
    prefix that would only confuse the model."""
    (tmp_path / "m.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    session = SessionStore(path=tmp_path / "s.sqlite").create(workspace=str(tmp_path))
    session.record_read("m.py", file_hash(tmp_path / "m.py"))
    context = ToolContext(workspace=tmp_path, session=session)

    with pytest.raises(EditError) as batched:
        build_edit_proposal(context, {"path": "m.py", "edits": [{"old_text": "x = 1", "new_text": "y"}]})
    with pytest.raises(EditError) as single:
        build_edit_proposal(context, {"path": "m.py", "old_text": "x = 1", "new_text": "y"})

    assert str(batched.value) == str(single.value)


def test_overlapping_replacements_are_rejected(workspace) -> None:
    tmp_path, context = workspace

    with pytest.raises(EditError) as excinfo:
        build_edit_proposal(
            context,
            {
                "path": "m.py",
                "edits": [
                    {"old_text": "alpha = 1\nbeta = 2", "new_text": "merged"},
                    {"old_text": "beta = 2", "new_text": "other"},
                ],
            },
        )

    assert "overlap" in str(excinfo.value)
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == SOURCE


def test_order_of_edits_does_not_matter(workspace) -> None:
    """Spans are sorted before splicing, so the model need not order its edits."""
    tmp_path, context = workspace

    proposal = build_edit_proposal(
        context,
        {
            "path": "m.py",
            "edits": [
                {"old_text": "gamma = 3", "new_text": "gamma = 30"},
                {"old_text": "alpha = 1", "new_text": "alpha = 10"},
            ],
        },
    )
    apply_edit(tmp_path, proposal)

    assert (tmp_path / "m.py").read_text(encoding="utf-8") == "alpha = 10\nbeta = 2\ngamma = 30\n"


def test_secret_in_any_replacement_is_refused(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret-value")
    tmp_path, context = workspace

    with pytest.raises(EditError, match="sensitive"):
        build_edit_proposal(
            context,
            {
                "path": "m.py",
                "edits": [
                    {"old_text": "alpha = 1", "new_text": "alpha = 10"},
                    {"old_text": "beta = 2", "new_text": "key = 'provider-secret-value'"},
                ],
            },
        )

    assert (tmp_path / "m.py").read_text(encoding="utf-8") == SOURCE


def test_malformed_edits_are_rejected(workspace) -> None:
    tmp_path, context = workspace

    for payload in ([], "nope", [{"new_text": "x"}]):
        with pytest.raises(EditError):
            build_edit_proposal(context, {"path": "m.py", "edits": payload})
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == SOURCE


def test_batch_edits_cannot_create_a_file(workspace) -> None:
    _tmp_path, context = workspace

    with pytest.raises(EditError, match="nothing to replace"):
        build_edit_proposal(
            context,
            {
                "path": "brand_new.py",
                "edits": [{"old_text": "a", "new_text": "b"}, {"old_text": "c", "new_text": "d"}],
            },
        )


def test_single_edit_form_still_works(workspace) -> None:
    """The single form is the length-1 case; it must not regress."""
    tmp_path, context = workspace

    proposal = build_edit_proposal(context, {"path": "m.py", "old_text": "beta = 2", "new_text": "beta = 20"})
    apply_edit(tmp_path, proposal)

    assert (tmp_path / "m.py").read_text(encoding="utf-8") == "alpha = 1\nbeta = 20\ngamma = 3\n"
