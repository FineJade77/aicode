import pytest

from app.policy.engine import PolicyEngine


@pytest.fixture
def engine():
    return PolicyEngine()


def gate_bash(engine, command, mode="default"):
    return engine.gate("bash", {"command": command}, mode=mode)


def test_read_only_tools_allowed_in_review(engine):
    assert engine.gate("read_file", {"path": "a.py"}, mode="review").verdict == "allow"
    assert engine.gate("search", {"query": "x"}, mode="review").verdict == "allow"


def test_write_tools_denied_in_review(engine):
    assert engine.gate("edit_file", {"path": "a.py"}, mode="review").verdict == "deny"
    assert gate_bash(engine, "ls", mode="review").verdict == "deny"


def test_edit_file_always_asks(engine):
    assert engine.gate("edit_file", {"path": "a.py"}).verdict == "ask"


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
