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


def test_gate_reason_localized_english(engine):
    # 默认（中文）
    zh = engine.gate("bash", {"command": "rm -rf /"})
    assert "禁止" in zh.reason
    # 英文会话
    en = engine.gate("bash", {"command": "rm -rf /"}, language="en-US")
    assert en.verdict == "deny"
    assert "not allowed" in en.reason and all(ord(c) < 128 for c in en.reason)
    # review 模式英文
    en_review = engine.gate("edit_file", {"path": "a.py"}, mode="review", language="en-US")
    assert en_review.verdict == "deny"
    assert "read-only" in en_review.reason


def test_gate_verdict_unaffected_by_language(engine):
    # 语言只影响 reason 文本，不影响判定
    for cmd in ["ls", "sed -i s/a/b/ f", "git push origin main", "rm -rf /"]:
        assert engine.gate("bash", {"command": cmd}).verdict == engine.gate("bash", {"command": cmd}, language="en-US").verdict
