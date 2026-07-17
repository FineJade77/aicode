import json
from pathlib import Path

from app.agent.steps import allowed_tool_names, build_planner_messages, choose_rule_step, observation_seen, parse_agent_step, related_test_queries, step_key


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


def test_choose_rule_step_reads_search_result_file() -> None:
    observations = bootstrap_observations()
    search_args = {"query": "login", "limit": 40}
    observations.append(
        {
            "tool": "search_text",
            "args": search_args,
            "step_key": step_key("search_text", search_args),
            "success": True,
            "data": {"workspace": "main", "matches": ["src/auth.py:12:def login():", "src/auth.py:20:return token"]},
        }
    )

    step = choose_rule_step("修复 login", "default", observations, [("search_text", search_args)])

    assert step.tool == "read_file"
    assert step.args == {"path": "src/auth.py", "max_bytes": 24_000}


def test_choose_rule_step_reads_find_files_result_from_workspace() -> None:
    observations = bootstrap_observations()
    find_args = {"workspace": "api", "query": "service.py", "limit": 20}
    observations.append(
        {
            "tool": "find_files",
            "args": find_args,
            "step_key": step_key("find_files", find_args),
            "success": True,
            "data": {"workspace": "api", "files": ["api:src/service.py"]},
        }
    )

    step = choose_rule_step("解释 api service", "default", observations, [("find_files", find_args)])

    assert step.tool == "read_file"
    assert step.args == {"path": "src/service.py", "max_bytes": 24_000, "workspace": "api"}


def test_choose_rule_step_locates_related_python_test_after_source_read() -> None:
    observations = bootstrap_observations()
    source_args = {"path": "src/calc.py", "max_bytes": 30_000}
    observations.append(
        {
            "tool": "read_file",
            "args": source_args,
            "step_key": step_key("read_file", source_args),
            "success": True,
            "data": {"workspace": "main", "path": "src/calc.py"},
        }
    )

    step = choose_rule_step("修复 src/calc.py", "default", observations, [])

    assert step.tool == "find_files"
    assert step.args == {"query": "test_calc.py", "limit": 20}


def test_choose_rule_step_reads_related_test_file_after_mapping() -> None:
    observations = bootstrap_observations()
    source_args = {"path": "src/calc.py", "max_bytes": 30_000}
    find_args = {"query": "test_calc.py", "limit": 20}
    observations.extend(
        [
            {
                "tool": "read_file",
                "args": source_args,
                "step_key": step_key("read_file", source_args),
                "success": True,
                "data": {"workspace": "main", "path": "src/calc.py"},
            },
            {
                "tool": "find_files",
                "args": find_args,
                "step_key": step_key("find_files", find_args),
                "success": True,
                "data": {"workspace": "main", "files": ["tests/test_calc.py"]},
            },
        ]
    )

    step = choose_rule_step("修复 src/calc.py", "default", observations, [])

    assert step.tool == "read_file"
    assert step.args == {"path": "tests/test_calc.py", "max_bytes": 24_000}


def test_related_test_queries_cover_first_batch_languages() -> None:
    assert related_test_queries("src/calc.py") == ["test_calc.py", "calc_test.py"]
    assert related_test_queries("pkg/calc.go") == ["calc_test.go"]
    assert related_test_queries("src/Button.tsx") == ["Button.test.tsx", "Button.spec.tsx"]
    assert related_test_queries("tests/test_calc.py") == []


def bootstrap_observations() -> list[dict]:
    observations = []
    for tool, args in [
        ("list_files", {"path": ".", "max_depth": 1, "limit": 40}),
        ("detect_project", {}),
        ("git_status", {}),
    ]:
        observations.append({"tool": tool, "args": args, "step_key": step_key(tool, args), "success": True, "data": {}})
    return observations


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
