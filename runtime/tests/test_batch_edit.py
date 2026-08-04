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


# --- one approval covering several files --------------------------------------
#
# Serial prompting asked about file 1 before the user had seen file 2. That is
# not a smaller version of the same question: a change only makes sense whole,
# and approving half of one is exactly the outcome nobody wants.

from app.agent.loop import prepare_edit_batch, selected_paths  # noqa: E402
from app.agent.session import ApprovalDecision  # noqa: E402
from app.models.provider import ToolCallRequest  # noqa: E402
from app.tools.runtime import DefaultToolRuntime  # noqa: E402


class RecordingBroker:
    """Answers with a fixed decision and remembers what it was asked."""

    def __init__(self, decision: ApprovalDecision, selection: tuple[str, ...] = ()) -> None:
        self.decision = decision
        self.selection = selection
        self.payloads: list[dict] = []

    async def request(self, session, *, kind, payload):
        self.payloads.append(payload)
        approval = session.create_approval(kind, payload)
        resolution = {
            ApprovalDecision.PARTIAL: "partial",
            ApprovalDecision.REVISE: "revise",
        }.get(self.decision, "")
        session.resolve_approval(
            approval.approval_id,
            accepted=self.decision in {ApprovalDecision.ACCEPTED, ApprovalDecision.PARTIAL},
            resolution=resolution,
            selection=self.selection,
        )
        return self.decision


def batch_runtime(broker):
    from app.agent.types import AgentRuntime

    return AgentRuntime(
        model_runtime=None,
        trace=None,
        tools=DefaultToolRuntime(),
        approvals=broker,
    )


def two_file_workspace(tmp_path: Path):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text(SOURCE, encoding="utf-8")
    session = SessionStore(path=tmp_path / "s.sqlite").create(workspace=str(tmp_path))
    context = ToolContext(workspace=tmp_path, session=session)
    for name in ("a.py", "b.py"):
        session.record_read(name, file_hash(tmp_path / name))
    return session, context


def edit_call(call_id: str, path: str, new_text: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id,
        name="edit_file",
        arguments={"path": path, "old_text": "alpha = 1", "new_text": new_text},
    )


@pytest.mark.asyncio
async def test_two_files_are_approved_in_one_request(tmp_path: Path) -> None:
    session, context = two_file_workspace(tmp_path)
    broker = RecordingBroker(ApprovalDecision.ACCEPTED)
    calls = [edit_call("c1", "a.py", "alpha = 10"), edit_call("c2", "b.py", "alpha = 20")]

    await prepare_edit_batch(session, calls, batch_runtime(broker), context)

    assert len(broker.payloads) == 1
    # Both diffs are in the one request, so the decision is made with the whole
    # change in view.
    assert broker.payloads[0]["paths"] == ["a.py", "b.py"]
    assert len(broker.payloads[0]["items"]) == 2
    assert all(entry["accepted"] for entry in session.pending_edit_batch.values())


@pytest.mark.asyncio
async def test_a_partial_approval_applies_only_what_was_picked(tmp_path: Path) -> None:
    session, context = two_file_workspace(tmp_path)
    broker = RecordingBroker(ApprovalDecision.PARTIAL, selection=("b.py",))
    calls = [edit_call("c1", "a.py", "alpha = 10"), edit_call("c2", "b.py", "alpha = 20")]

    await prepare_edit_batch(session, calls, batch_runtime(broker), context)

    assert session.pending_edit_batch["c1"]["accepted"] is False
    assert session.pending_edit_batch["c2"]["accepted"] is True


@pytest.mark.asyncio
async def test_two_edits_to_one_file_stay_serial(tmp_path: Path) -> None:
    """The second is built against what the first produced.

    Showing both up front would display a diff that no longer applies by the
    time it runs.
    """
    session, context = two_file_workspace(tmp_path)
    broker = RecordingBroker(ApprovalDecision.ACCEPTED)
    calls = [edit_call("c1", "a.py", "alpha = 10"), edit_call("c2", "a.py", "alpha = 20")]

    await prepare_edit_batch(session, calls, batch_runtime(broker), context)

    assert broker.payloads == []
    assert session.pending_edit_batch == {}


@pytest.mark.asyncio
async def test_a_single_edit_is_not_batched(tmp_path: Path) -> None:
    session, context = two_file_workspace(tmp_path)
    broker = RecordingBroker(ApprovalDecision.ACCEPTED)

    await prepare_edit_batch(session, [edit_call("c1", "a.py", "alpha = 10")], batch_runtime(broker), context)

    assert broker.payloads == []


@pytest.mark.asyncio
async def test_accept_all_skips_the_batch_prompt(tmp_path: Path) -> None:
    session, context = two_file_workspace(tmp_path)
    session.auto_accept_edits = True
    broker = RecordingBroker(ApprovalDecision.ACCEPTED)
    calls = [edit_call("c1", "a.py", "alpha = 10"), edit_call("c2", "b.py", "alpha = 20")]

    await prepare_edit_batch(session, calls, batch_runtime(broker), context)

    assert broker.payloads == []


def test_an_unrecognisable_selection_approves_nothing(tmp_path: Path) -> None:
    """Applying everything because the subset failed to parse is the one outcome
    a partial approval exists to prevent."""
    session = SessionStore(path=tmp_path / "s.sqlite").create(workspace=str(tmp_path))
    approval = session.create_approval("edit", {})
    session.resolve_approval(
        approval.approval_id, accepted=True, resolution="partial", selection=("nowhere.py",)
    )

    assert selected_paths(session, ApprovalDecision.PARTIAL, ["a.py", "b.py"]) == []


def test_a_full_acceptance_selects_everything(tmp_path: Path) -> None:
    session = SessionStore(path=tmp_path / "s.sqlite").create(workspace=str(tmp_path))

    assert selected_paths(session, ApprovalDecision.ACCEPTED, ["a.py"]) == ["a.py"]
    assert selected_paths(session, ApprovalDecision.REJECTED, ["a.py"]) == []
