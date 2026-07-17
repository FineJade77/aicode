import json
from pathlib import Path

from app.agent.steps import allowed_tool_names, build_planner_messages, choose_rule_step, observation_seen, parse_agent_step, step_key


def test_parse_agent_step_accepts_fenced_json() -> None:
    step = parse_agent_step(
        """```json
{"action":"tool","tool":"read_file","args":{"path":"README.md"},"reason":"need file context"}
```""",
        allowed_tool_names("default"),
    )

    assert step is not None
    assert step.action == "tool"
    assert step.tool == "read_file"
    assert step.args == {"path": "README.md"}
    assert step.source == "model"


def test_parse_agent_step_rejects_unknown_tool() -> None:
    step = parse_agent_step(
        '{"action":"tool","tool":"run_shell","args":{"command":"rm -rf build"}}',
        allowed_tool_names("review"),
    )

    assert step is None


def test_choose_rule_step_runs_bootstrap_then_context() -> None:
    observations = []
    context_tools = [("git_diff", {})]

    first = choose_rule_step("看看 diff", "default", observations, context_tools)
    assert first.tool == "list_files"
    observations.append({"tool": first.tool, "step_key": step_key(first.tool, first.args)})

    second = choose_rule_step("看看 diff", "default", observations, context_tools)
    assert second.tool == "detect_project"
    observations.append({"tool": second.tool, "step_key": step_key(second.tool, second.args)})

    third = choose_rule_step("看看 diff", "default", observations, context_tools)
    assert third.tool == "git_status"
    observations.append({"tool": third.tool, "step_key": step_key(third.tool, third.args)})

    fourth = choose_rule_step("看看 diff", "default", observations, context_tools)
    assert fourth.tool == "git_diff"
    observations.append({"tool": fourth.tool, "step_key": step_key(fourth.tool, fourth.args)})

    finish = choose_rule_step("看看 diff", "default", observations, context_tools)
    assert finish.action == "finish"


def test_review_mode_allowed_tools_are_read_only() -> None:
    assert "review_diff" in allowed_tool_names("review")
    assert "run_tests" not in allowed_tool_names("review")


def test_observation_seen_uses_stable_step_key() -> None:
    args = {"path": "README.md", "max_bytes": 1000}
    observations = [{"step_key": step_key("read_file", args)}]

    assert observation_seen(observations, "read_file", {"max_bytes": 1000, "path": "README.md"})


def test_build_planner_messages_lists_allowed_tools() -> None:
    messages = build_planner_messages(
        language="zh-CN",
        message="解释 README.md",
        mode="default",
        workspace="/repo",
        observations=[],
    )

    assert messages[0]["role"] == "system"
    assert "JSON object" in messages[0]["content"]
    assert "read_file" in messages[1]["content"]
    assert "解释 README.md" in messages[1]["content"]


def test_build_planner_messages_includes_configured_workspaces(tmp_path: Path) -> None:
    config_dir = tmp_path / ".aicode"
    config_dir.mkdir()
    (config_dir / "config.json").write_text('{"workspaces":[{"name":"api","path":"../api","mode":"read_only"}]}', encoding="utf-8")

    messages = build_planner_messages(
        language="zh-CN",
        message="解释 api:README.md",
        mode="default",
        workspace=str(tmp_path),
        observations=[],
    )
    payload = json.loads(messages[1]["content"])

    assert payload["configured_workspaces"] == [{"name": "api", "mode": "read_only"}]
