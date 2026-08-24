from __future__ import annotations

from pathlib import Path

import pytest

from app.sessions.store import SessionStore
from app.tools.base import ToolContext
from app.tools.edit import EditError, build_edit_proposal
from app.tools.registry import DEFAULT_REGISTRY


def context(session, tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=tmp_path, session=session)


async def read(session, tmp_path: Path, name: str):
    return await DEFAULT_REGISTRY.run("read_file", {"path": name}, context(session, tmp_path))


@pytest.fixture
def session(tmp_path: Path):
    return SessionStore(path=tmp_path / "s.sqlite").create(workspace=str(tmp_path))


@pytest.mark.asyncio
async def test_editing_an_unread_file_is_refused(session, tmp_path: Path) -> None:
    """`base_hash` catches "read, then changed externally". It cannot catch
    "never read at all" — the model can invent `old_text`, and the resulting
    mismatch is indistinguishable to it from "the file simply differs"."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(EditError) as excinfo:
        build_edit_proposal(context(session, tmp_path), {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"})

    message = str(excinfo.value)
    assert "read_file" in message, "the refusal must name the action that unblocks it"
    assert "a.py" in message
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"


@pytest.mark.asyncio
async def test_reading_first_unlocks_the_edit(session, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    await read(session, tmp_path, "a.py")

    proposal = build_edit_proposal(
        context(session, tmp_path), {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}
    )

    assert proposal.kind == "replace"


@pytest.mark.asyncio
async def test_deleting_an_unread_file_is_refused(session, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(EditError, match="read_file"):
        build_edit_proposal(context(session, tmp_path), {"path": "a.py", "delete": True})

    await read(session, tmp_path, "a.py")
    assert build_edit_proposal(context(session, tmp_path), {"path": "a.py", "delete": True}).kind == "delete"


def test_creating_a_new_file_needs_no_prior_read(session, tmp_path: Path) -> None:
    """There is nothing to have read."""
    proposal = build_edit_proposal(context(session, tmp_path), {"path": "new/b.py", "new_text": "print(1)\n"})

    assert proposal.kind == "create"


@pytest.mark.asyncio
async def test_reads_of_secondary_workspaces_do_not_unlock_edits(session, tmp_path: Path) -> None:
    """Only the primary workspace is editable, so only its reads count."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    await read(session, tmp_path, "a.py")

    assert session.read_hash("a.py") is not None


@pytest.mark.asyncio
async def test_enforcement_is_skipped_without_a_session(tmp_path: Path) -> None:
    """An embedder driving the edit tools directly has no session-scoped state to
    check against; the Agent Loop always supplies one."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    proposal = build_edit_proposal(
        ToolContext(workspace=tmp_path), {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}
    )

    assert proposal.kind == "replace"
