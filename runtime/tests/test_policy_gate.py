import os

import pytest

from app.agent.policy import PolicyEngine
from app.tools.registry import DEFAULT_REGISTRY


@pytest.fixture
def engine():
    return PolicyEngine()


def gate_tool(engine, name, args, mode="default", **kwargs):
    """Gate a tool the way the Agent Loop does.

    read_only comes from the tool's own ToolSpec rather than being hardcoded
    here, so these tests also verify that the declaration is right — the policy
    engine no longer keeps its own copy of the read-only names.
    """
    return engine.gate(name, args, mode=mode, spec=DEFAULT_REGISTRY.spec_for(name), **kwargs)


def gate_bash(engine, command, mode="default", **kwargs):
    return gate_tool(engine, "bash", {"command": command}, mode=mode, **kwargs)


def test_read_only_tools_allowed_in_review(engine):
    assert gate_tool(engine, "read_file", {"path": "a.py"}, mode="review").verdict == "allow"
    assert gate_tool(engine, "search", {"query": "x"}, mode="review").verdict == "allow"
    assert gate_tool(engine, "related_files", {"path": "a.py"}, mode="review").verdict == "allow"


def test_write_tools_denied_in_review(engine):
    assert gate_tool(engine, "edit_file", {"path": "a.py"}, mode="review").verdict == "deny"
    assert gate_bash(engine, "ls", mode="review").verdict == "deny"


def test_write_tools_denied_in_commit_message_mode(engine):
    assert gate_tool(engine, "read_file", {"path": "a.py"}, mode="commit_message").verdict == "allow"
    assert gate_tool(engine, "edit_file", {"path": "a.py"}, mode="commit_message").verdict == "deny"
    assert gate_bash(engine, "git diff", mode="commit_message").verdict == "deny"


def test_read_only_tools_allowed_in_explain(engine):
    assert gate_tool(engine, "read_file", {"path": "a.py"}, mode="explain").verdict == "allow"
    assert gate_tool(engine, "search", {"query": "x"}, mode="explain").verdict == "allow"
    assert gate_tool(engine, "related_files", {"path": "a.py"}, mode="explain").verdict == "allow"


def test_write_tools_denied_in_explain_mode(engine):
    assert gate_tool(engine, "edit_file", {"path": "a.py"}, mode="explain").verdict == "deny"
    assert gate_bash(engine, "ls", mode="explain").verdict == "deny"


def test_edit_file_always_asks(engine):
    assert gate_tool(engine, "edit_file", {"path": "a.py"}).verdict == "ask"


def test_low_risk_commands_allowed(engine):
    for command in ["ls", "pwd", "rg pattern", "cat a.py", "git status", "git diff", "git log -5", "pytest", "go test ./...", "python3 -m pytest"]:
        assert gate_bash(engine, command).verdict == "allow", command


def test_sed_requires_confirmation(engine):
    assert gate_bash(engine, "sed -i 's/a/b/' file.py").verdict == "ask"


def test_git_push_requires_confirmation(engine):
    assert gate_bash(engine, "git push origin main").verdict == "ask"
    assert gate_bash(engine, "git commit -m x").verdict == "ask"


def test_destructive_commands_denied(engine):
    for command in [
        "rm -rf /",
        "sudo ls",
        "git reset --hard",
        "git checkout -- .",
        "git clean -fd",
        "git rebase main",
        "git push --force origin main",
        "git push -f origin main",
        "git branch -D feature",
        "git stash drop",
    ]:
        assert gate_bash(engine, command).verdict == "deny", command


def test_control_tokens_ask(engine):
    assert gate_bash(engine, "cat a.py | head").verdict == "ask"
    assert gate_bash(engine, "echo hi > out.txt").verdict == "ask"


def test_unknown_command_asks(engine):
    assert gate_bash(engine, "docker build .").verdict == "ask"
    assert gate_bash(engine, "npm install").verdict == "ask"


def test_unknown_tool_denied(engine):
    assert engine.gate("mystery", {}).verdict == "deny"


def test_newline_chained_deny_command_is_denied(engine):
    assert gate_bash(engine, "ls\nrm -rf /").verdict == "deny"


def test_bare_ampersand_chained_deny_command_is_denied(engine):
    assert gate_bash(engine, "ls & rm -rf /").verdict == "deny"


def test_deny_wins_over_control_token_precedence(engine):
    assert gate_bash(engine, "rm -rf / && true").verdict == "deny"


def test_deny_wins_after_semicolon_separator(engine):
    assert gate_bash(engine, "git push --force origin main; echo done").verdict == "deny"


def test_path_and_env_prefix_do_not_bypass_deny_list(engine):
    assert gate_bash(engine, "/bin/rm -rf /").verdict == "deny"
    assert gate_bash(engine, "FOO=1 rm -rf /").verdict == "deny"


def test_benign_chained_commands_stay_allowed(engine):
    assert gate_bash(engine, "ls && pwd").verdict == "allow"
    assert gate_bash(engine, "pytest && echo done").verdict == "allow"


def test_background_operator_requires_confirmation(engine):
    assert gate_bash(engine, "pytest & echo done").verdict == "ask"
    assert gate_bash(engine, "sleep 60 &").verdict == "ask"


def test_remaining_control_and_redirection_tokens_force_ask(engine):
    for command in [
        "ls; pwd",
        "ls || pwd",
        "echo hi > out.txt",
        "echo hi >> out.txt",
        "cat < in.txt",
        "cat `whoami`",
        "cat $(whoami)",
    ]:
        assert gate_bash(engine, command).verdict == "ask", command


def test_escaped_and_quoted_separators_do_not_hard_fail(engine):
    # `\;` is a literal argument to `find`, not a shell separator; `find`
    # is not on the allowlist so the (correctly split) sub-command asks.
    assert gate_bash(engine, r"find . -exec rm {} \;").verdict == "ask"
    # `&&` here is inside a double-quoted echo argument, not a real
    # separator; still surfaced as "ask" out of caution since a
    # quote-aware split proves it merely looks like a control operator.
    assert gate_bash(engine, 'echo "a && b"').verdict == "ask"
    # `&` here is inside a quoted filename argument to `ls`, not a real
    # separator, and single `&`/`|`/`;` embedded in quoted text is fully
    # trusted, so this is a single benign `ls` call.
    assert gate_bash(engine, 'ls "foo & rm -rf /"').verdict == "allow"


def test_quoted_semicolon_in_git_commit_message_stays_single_command(engine):
    assert gate_bash(engine, 'git commit -m "fix: a; b"').verdict == "ask"


def test_git_via_absolute_or_qualified_path_still_denies(engine):
    for command in [
        "/usr/bin/git reset --hard",
        "/usr/bin/git push --force origin main",
        "/opt/homebrew/bin/git branch -D feature",
    ]:
        assert gate_bash(engine, command).verdict == "deny", command


def test_env_and_command_wrappers_do_not_bypass_deny_list(engine):
    for command in [
        "/usr/bin/env rm -rf /",
        "env -i rm -rf /",
        "/usr/bin/env -S rm -rf /",
    ]:
        assert gate_bash(engine, command).verdict == "deny", command


def test_prior_bypass_inputs_still_deny_after_quote_aware_split(engine):
    assert gate_bash(engine, "ls\nrm -rf /").verdict == "deny"
    assert gate_bash(engine, "rm -rf / && true").verdict == "deny"


def test_gate_reason_is_english(engine):
    decision = gate_tool(engine, "bash", {"command": "rm -rf /"})
    assert decision.verdict == "deny"
    assert "not allowed" in decision.reason and all(ord(char) < 128 for char in decision.reason)
    review = gate_tool(engine, "edit_file", {"path": "a.py"}, mode="review")
    assert review.verdict == "deny"
    assert "read-only" in review.reason


def test_untrusted_workspace_requires_approval_for_project_commands(engine, tmp_path):
    for command in ["pytest", "go test ./...", "npm test", "python3 -m pytest"]:
        decision = gate_bash(engine, command, workspace=tmp_path, trust_level="untrusted")
        assert decision.verdict == "ask", command
        assert "untrusted" in decision.reason


def test_trusted_workspace_allows_low_risk_project_commands(engine, tmp_path):
    assert gate_bash(engine, "pytest", workspace=tmp_path, trust_level="trusted").verdict == "allow"


def test_shell_denies_workspace_and_sensitive_path_escapes(engine, tmp_path):
    outside = tmp_path.parent / "outside-secret"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "key.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "escape-link").symlink_to(outside)

    commands = [
        "cat .env",
        "cat ../outside-secret",
        f"cat {outside}",
        "cat secrets/key.txt",
        "cat escape-link",
        "sed -n 1p ~/.ssh/config",
        "bash -c 'cat ../outside-secret'",
    ]
    for command in commands:
        decision = gate_bash(
            engine,
            command,
            workspace=tmp_path,
            protected_paths=["secrets/**"],
            trust_level="trusted",
        )
        assert decision.verdict == "deny", (command, decision)


def test_shell_allows_paths_resolved_inside_workspace(engine, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text("print('ok')", encoding="utf-8")

    assert gate_bash(engine, "cat src/a.py", workspace=tmp_path).verdict == "allow"
    assert gate_bash(engine, "/bin/cat src/a.py", workspace=tmp_path).verdict == "allow"


def test_shell_path_guard_expands_globs_before_execution(engine, tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")

    decision = gate_bash(engine, "cat .e*", workspace=tmp_path)

    assert decision.verdict == "deny"


def test_shell_path_guard_checks_env_wrapper_path_flags(engine, tmp_path):
    outside = tmp_path.parent / "outside-env-cwd"
    outside.mkdir(exist_ok=True)

    decision = gate_bash(engine, f"env --chdir={outside} cat harmless.txt", workspace=tmp_path)

    assert decision.verdict == "deny"


def test_shell_rejects_literal_runtime_secret(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret-value")

    decision = gate_bash(engine, "printf provider-secret-value", workspace=tmp_path)

    assert decision.verdict == "deny"
    assert "sensitive Runtime environment value" in decision.reason


def test_shell_rejects_path_qualified_executable_outside_workspace(engine, tmp_path):
    fake_cat = tmp_path.parent / "cat"
    fake_cat.write_text("#!/bin/sh\n", encoding="utf-8")

    decision = gate_bash(engine, f"{fake_cat} harmless.txt", workspace=tmp_path)

    assert decision.verdict == "deny"


def test_untrusted_shell_does_not_auto_allow_workspace_path_hijack(
    engine,
    tmp_path,
    monkeypatch,
):
    fake_ls = tmp_path / "ls"
    fake_ls.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_ls.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    decision = gate_bash(engine, "ls", workspace=tmp_path, trust_level="untrusted")

    assert decision.verdict == "ask"
