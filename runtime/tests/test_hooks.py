"""Project-declared hooks.

The property that matters most here is not that hooks run — it is that they run
*through* the same execution, policy and trust checks as a command the model
asked for. A hook that skipped those would be a way to execute anything with
none of the checks, declared in a file that ships with the repository.
"""

from __future__ import annotations

import json

import pytest

from app.agent.loop import run_turn
from app.execution.service import ExecutionService
from app.project.config import HookRef, parse_hooks, parse_project_config
from app.tools.base import ToolContext
from app.tools.edit import file_hash
from app.tools.hooks import matching_hooks, render_hook_command, run_hooks
from app.tools.registry import DEFAULT_REGISTRY, build_tool_context


def context_for(tmp_path, hooks, *, trust_level="trusted", execution=None):
    return ToolContext(
        workspace=tmp_path,
        execution=execution or ExecutionService(),
        trust_level=trust_level,
        bash_backend="host",
        hooks=hooks,
    )


# --- configuration ----------------------------------------------------------


def test_an_unknown_event_is_dropped_rather_than_defaulted():
    """A typo must not attach a command to a different trigger than was written."""
    hooks = parse_hooks(
        [
            {"event": "post_editt", "command": "echo typo"},
            {"event": "post_edit", "command": "echo real"},
        ]
    )
    assert [hook.command for hook in hooks] == ["echo real"]


def test_malformed_entries_are_dropped():
    hooks = parse_hooks(["not-an-object", {"event": "pre_bash"}, {"command": "echo x"}, 42])
    assert hooks == []


def test_hook_defaults_and_limits():
    (hook,) = parse_hooks([{"event": "pre_bash", "command": "x", "timeout": 10_000}])
    assert hook.timeout == 600.0
    assert hook.blocking is True
    (unbounded,) = parse_hooks([{"event": "pre_bash", "command": "x", "timeout": -1}])
    assert unbounded.timeout == 60.0


def test_hooks_are_read_from_project_config():
    config = parse_project_config(
        {"hooks": [{"event": "post_edit", "command": "fmt {path}", "match": "*.py"}]}
    )
    assert config.hooks[0].event == "post_edit"
    assert config.hooks[0].match == "*.py"


def test_build_tool_context_carries_project_hooks(tmp_path):
    (tmp_path / ".aicode").mkdir()
    (tmp_path / ".aicode" / "config.json").write_text(
        json.dumps({"hooks": [{"event": "post_edit", "command": "echo hi"}]}), encoding="utf-8"
    )
    context = build_tool_context(str(tmp_path), "default")
    assert [hook.command for hook in context.hooks] == ["echo hi"]


# --- matching ---------------------------------------------------------------


def test_a_bare_extension_pattern_matches_nested_files():
    """`*.py` reads as "python files", not "python files in the root"."""
    hooks = [HookRef(event="post_edit", command="fmt", match="*.py")]
    assert matching_hooks(hooks, "post_edit", "src/deep/app.py")
    assert not matching_hooks(hooks, "post_edit", "src/app.ts")


def test_an_empty_match_selects_everything():
    hooks = [HookRef(event="pre_bash", command="lint")]
    assert matching_hooks(hooks, "pre_bash", "git commit -m x")


def test_events_do_not_cross():
    hooks = [HookRef(event="pre_bash", command="lint")]
    assert matching_hooks(hooks, "post_edit", "a.py") == []


# --- substitution -----------------------------------------------------------


def test_substituted_values_are_shell_quoted():
    """A repo can contain a file named `a; rm -rf ~.py`.

    Pasting that in raw turns "format what I just wrote" into arbitrary command
    execution, so the substitution quotes rather than concatenates.
    """
    rendered = render_hook_command("fmt {path}", {"path": "a; rm -rf ~.py"})
    assert "; rm -rf" not in rendered.replace("'a; rm -rf ~.py'", "")
    assert rendered == "fmt 'a; rm -rf ~.py'"


@pytest.mark.asyncio
async def test_a_hostile_filename_cannot_run_a_second_command(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("intact", encoding="utf-8")
    hooks = [HookRef(event="post_edit", command="echo {path} > /dev/null")]

    await run_hooks(
        context_for(tmp_path, hooks),
        "post_edit",
        target="x.py",
        values={"path": f"x.py; rm -f {victim}"},
    )

    assert victim.read_text(encoding="utf-8") == "intact"


# --- trust ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hooks_do_not_run_in_an_untrusted_workspace(tmp_path):
    """Cloning a hostile repository must not be enough to execute its commands.

    `.aicode/config.json` ships with the repository, so a hook is code chosen by
    whoever wrote it.
    """
    marker = tmp_path / "hook-ran"
    hooks = [HookRef(event="post_edit", command=f"touch {marker}")]

    outcome = await run_hooks(
        context_for(tmp_path, hooks, trust_level="untrusted"), "post_edit", target="a.py"
    )

    assert not marker.exists()
    assert outcome.ran == []
    # Announced rather than silently skipped: a hook that did not fire must not
    # be mistaken for one that passed.
    assert "not trusted" in outcome.reason


# --- policy -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hook_cannot_reach_past_policy(tmp_path):
    """Putting a command in a config file is not a way around the deny list."""
    hooks = [HookRef(event="pre_bash", command="rm -rf /")]

    outcome = await run_hooks(context_for(tmp_path, hooks), "pre_bash", target="anything")

    assert not outcome.ok
    assert "denied by policy" in outcome.reason


# --- behaviour --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_post_edit_hook_runs_and_reports(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("unformatted", encoding="utf-8")
    hooks = [HookRef(event="post_edit", command="printf formatted > {path}", match="*.py")]

    outcome = await run_hooks(
        context_for(tmp_path, hooks), "post_edit", target="a.py", values={"path": "a.py"}
    )

    assert outcome.ok
    assert target.read_text(encoding="utf-8") == "formatted"


@pytest.mark.asyncio
async def test_a_failing_post_edit_hook_does_not_block(tmp_path):
    """The edit is already on disk; a formatter's job is not to gate it."""
    hooks = [HookRef(event="post_edit", command="exit 1")]

    outcome = await run_hooks(context_for(tmp_path, hooks), "post_edit", target="a.py")

    assert outcome.ok
    assert "failed" in outcome.output


@pytest.mark.asyncio
async def test_a_failing_pre_bash_hook_blocks(tmp_path):
    hooks = [HookRef(event="pre_bash", command="exit 2", match="git commit*")]

    outcome = await run_hooks(
        context_for(tmp_path, hooks), "pre_bash", target="git commit -m x"
    )

    assert not outcome.ok
    assert "exit=2" in outcome.reason


@pytest.mark.asyncio
async def test_a_non_matching_pre_bash_hook_lets_the_command_through(tmp_path):
    hooks = [HookRef(event="pre_bash", command="exit 2", match="git commit*")]

    outcome = await run_hooks(context_for(tmp_path, hooks), "pre_bash", target="ls -la")

    assert outcome.ok
    assert outcome.ran == []


@pytest.mark.asyncio
async def test_a_non_blocking_pre_bash_hook_only_reports(tmp_path):
    hooks = [HookRef(event="pre_bash", command="exit 3", blocking=False)]

    outcome = await run_hooks(context_for(tmp_path, hooks), "pre_bash", target="ls")

    assert outcome.ok


# --- integration through the bash tool --------------------------------------


@pytest.mark.asyncio
async def test_the_bash_tool_refuses_a_command_its_hook_rejected(tmp_path):
    # Relative paths on purpose: an absolute tmp_path carries the test's own
    # name, which made the match glob fire on the setup command instead of the
    # one under test.
    hooks = [HookRef(event="pre_bash", command="exit 1", match="git commit*")]
    context = context_for(tmp_path, hooks)

    allowed = await DEFAULT_REGISTRY.run("bash", {"command": "touch allowed.txt"}, context)
    assert allowed.success, allowed.error
    assert (tmp_path / "allowed.txt").exists()

    blocked = await DEFAULT_REGISTRY.run(
        "bash", {"command": "git commit -m x && touch slipped-through.txt"}, context
    )
    assert not blocked.success
    assert blocked.data["status"] == "blocked_by_hook"
    # Refused, not merely reported: the command must not have run at all.
    assert not (tmp_path / "slipped-through.txt").exists()


@pytest.mark.asyncio
async def test_hook_execution_is_audited_like_an_agent_command(tmp_path):
    """Same execution path means the same audit trail, tagged by event."""

    class RecordingAudit:
        def __init__(self):
            self.records = []

        def record(self, event_type, **kwargs):
            self.records.append((event_type, kwargs))

    audit = RecordingAudit()
    service = ExecutionService(audit=audit)
    hooks = [HookRef(event="pre_bash", command="true")]

    await run_hooks(
        context_for(tmp_path, hooks, execution=service), "pre_bash", target="ls"
    )

    actions = {
        (kwargs.get("data") or {}).get("action")
        for _event, kwargs in audit.records
        if isinstance(kwargs.get("data"), dict)
    }
    assert "hook.pre_bash" in actions


# --- end to end through the Agent Loop --------------------------------------


@pytest.mark.asyncio
async def test_a_post_edit_hook_reformats_the_file_the_model_just_wrote(tmp_path):
    """The whole point of post_edit, exercised through the real loop.

    Also covers the consequence that is easy to miss: the hook rewrote the file,
    so the read record has to be refreshed or the model's next edit to it would
    be rejected as stale.
    """
    import asyncio

    from tests.test_loop import (
        Request,
        events_of,
        make_runtime,
        make_session,
        mark_read,
        text_turn,
        tool_turn,
        without_verification,
    )

    (tmp_path / ".aicode").mkdir()
    (tmp_path / ".aicode" / "config.json").write_text(
        json.dumps(
            {"hooks": [{"event": "post_edit", "command": "printf 'formatted\\n' > {path}", "match": "*.py"}]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, _ = make_runtime(
        [
            tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}),
            text_turn("Changed"),
            text_turn("Complete"),
        ],
        tmp_path,
        settings=without_verification(),
    )
    session = make_session(tmp_path)
    mark_read(session, tmp_path, "a.py")

    async def approve_soon():
        for _ in range(200):
            await asyncio.sleep(0.01)
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=True)
                return

    _task = asyncio.create_task(approve_soon())
    await run_turn(session, Request(tmp_path), runtime)

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "formatted\n"
    finished = events_of(session, "hook.finished")
    assert finished and finished[0]["event"] == "post_edit"
    assert finished[0]["path"] == "a.py"
    # Refreshed to what the hook left behind, not to what the edit applied.
    assert session.read_hash("a.py") == file_hash(tmp_path / "a.py")
