import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.project.detect import detect_test_command
from app.server.main import (
    MessageRequest,
    audit,
    bind_message_request_to_session,
    build_model_messages,
    detect_append_request,
    detect_create_request,
    detect_replace_request,
    detect_shell_request,
    execute_tool,
    emit_run_queued,
    final_summary_text,
    model_purpose_for_mode,
    model_routes,
    process_session_runs,
    review_rules,
    run_post_patch_verification,
)
from app.sessions.store import Session


def test_detect_test_command_for_go_work(tmp_path: Path) -> None:
    (tmp_path / "go.work").write_text("go 1.22\n\nuse ./cli\n", encoding="utf-8")

    assert detect_test_command(tmp_path) == "go test ./cli/..."


def test_detect_append_request() -> None:
    assert detect_append_request("append README.md hello world") == ("README.md", "hello world")
    assert detect_append_request("追加 README.md 你好") == ("README.md", "你好")


def test_detect_replace_request() -> None:
    assert detect_replace_request("replace README.md old value => new value") == ("README.md", "old value", "new value")
    assert detect_replace_request("替换 README.md 旧值 -> 新值") == ("README.md", "旧值", "新值")
    assert detect_replace_request("replace README.md missing separator") is None


def test_detect_create_request() -> None:
    assert detect_create_request("create TODO.md hello world") == ("TODO.md", "hello world")
    assert detect_create_request("创建 notes/today.md 你好") == ("notes/today.md", "你好")
    assert detect_create_request("create TODO.md") is None


def test_detect_shell_request() -> None:
    assert detect_shell_request("shell python3 -m pytest") == "python3 -m pytest"
    assert detect_shell_request("运行命令 python3 -m pytest") == "python3 -m pytest"
    assert detect_shell_request("执行命令 go test ./...") == "go test ./..."


def test_message_request_is_bound_to_session_context(tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="hello", mode="default", workspace=str(tmp_path), language="en")

    effective = bind_message_request_to_session(session, request)

    assert effective.workspace == session.workspace
    assert effective.language == "zh-CN"
    assert request.language == "en"


def test_message_request_rejects_workspace_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    session = Session(session_id="sess_test", workspace=str(repo), language="zh-CN")
    request = MessageRequest(message="hello", mode="default", workspace=str(other), language="zh-CN")

    with pytest.raises(HTTPException) as exc_info:
        bind_message_request_to_session(session, request)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_process_session_runs_serializes_queued_messages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    first = MessageRequest(message="first", mode="default", workspace=str(tmp_path), language="zh-CN")
    second = MessageRequest(message="second", mode="default", workspace=str(tmp_path), language="zh-CN")
    first_run = session.enqueue_agent_run(first)
    second_run = session.enqueue_agent_run(second)
    active = 0
    seen: list[str] = []

    async def fake_run_agent(target_session: Session, request: MessageRequest) -> None:
        nonlocal active
        active += 1
        assert active == 1
        seen.append(request.message)
        await target_session.events.put({"type": "final", "summary": request.message})
        active -= 1

    monkeypatch.setattr("app.server.main.run_agent", fake_run_agent)

    await process_session_runs(session)

    finals = [event for event in session.events.events_after(0) if event["type"] == "final"]
    assert seen == ["first", "second"]
    assert [event["summary"] for event in finals] == ["first", "second"]
    assert [event["run_id"] for event in finals] == [first_run.run_id, second_run.run_id]


@pytest.mark.asyncio
async def test_emit_run_queued_marks_queued_run(tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="hello", mode="default", workspace=str(tmp_path), language="zh-CN")
    queued = session.enqueue_agent_run(request)

    await emit_run_queued(session, queued, was_running=True, queue_position=2)

    event = await asyncio.wait_for(session.events.get(), timeout=1)
    assert event["type"] == "run.queued"
    assert event["run_id"] == queued.run_id
    assert event["status"] == "queued"
    assert event["queue_position"] == 2


def test_review_mode_uses_reviewer_model_purpose() -> None:
    assert model_purpose_for_mode("review") == "reviewer"
    assert model_purpose_for_mode("default") == "summarizer"


def test_review_model_prompt_includes_tool_observations() -> None:
    request = MessageRequest(message="请审查当前代码变更。", mode="review", workspace="/repo", language="zh-CN")
    messages = build_model_messages(
        request,
        [
            {
                "tool": "review_diff",
                "success": True,
                "risk_level": "low",
                "requires_approval": False,
                "text": "Review 结果: 未发现确定性风险。",
                "data": {"summary": {"finding_count": 0}},
            }
        ],
    )

    assert messages[0]["role"] == "system"
    assert "只读代码审查助手" in messages[0]["content"]
    assert "review_diff" in messages[1]["content"]
    assert "finding_count" in messages[1]["content"]


def test_review_final_falls_back_to_deterministic_text_for_stub() -> None:
    request = MessageRequest(message="请审查当前代码变更。", mode="review", workspace="/repo", language="zh-CN")
    summary = final_summary_text(
        request,
        [
            {"tool": "detect_project", "success": True, "data": {"test_command": "go test ./..."}},
            {
                "tool": "review_diff",
                "success": True,
                "text": "Review 结果: 未发现确定性风险。",
                "data": {"summary": {"finding_count": 0, "by_severity": {"high": 0, "medium": 0, "low": 0}}, "findings": []},
            },
        ],
        "Runtime 骨架已连接。",
        "stub",
    )

    assert "模型 provider 未配置" in summary
    assert "结论\n未发现确定性风险。" in summary
    assert "风险\n- 无必须处理问题。" in summary
    assert "建议验证\n- 运行 `go test ./...`。" in summary


def test_review_final_groups_findings_for_stub() -> None:
    request = MessageRequest(message="请审查当前代码变更。", mode="review", workspace="/repo", language="zh-CN")
    summary = final_summary_text(
        request,
        [
            {"tool": "detect_project", "success": True, "data": {"test_command": "python3 -m pytest"}},
            {
                "tool": "review_diff",
                "success": True,
                "data": {
                    "summary": {"finding_count": 1, "by_severity": {"high": 1, "medium": 0, "low": 0}},
                    "findings": [
                        {
                            "severity": "high",
                            "path": ".env",
                            "line": 2,
                            "title": "新增行包含疑似密钥",
                            "message": "新增内容匹配凭证或私钥特征。",
                        }
                    ],
                },
            },
        ],
        "Runtime 骨架已连接。",
        "stub",
    )

    assert "发现 1 个确定性问题：high=1 medium=0 low=0。" in summary
    assert "- [high] .env:2 新增行包含疑似密钥：新增内容匹配凭证或私钥特征。" in summary
    assert "- 运行 `python3 -m pytest`。" in summary


def test_patch_final_reports_applied_and_verification_passed_for_stub() -> None:
    request = MessageRequest(message="replace sample.txt old => new", mode="default", workspace="/repo", language="zh-CN")
    summary = final_summary_text(
        request,
        [
            {
                "tool": "apply_patch",
                "success": True,
                "status": "applied",
                "operation": "replace",
                "files": ["sample.txt"],
                "verification": {"status": "passed", "command": "python3 -m pytest"},
            }
        ],
        "Runtime 骨架已连接。",
        "stub",
    )

    assert "Patch 结果" in summary
    assert "已执行 `replace`，文件: sample.txt。" in summary
    assert "验证通过: `python3 -m pytest`。" in summary


def test_patch_final_reports_verification_skipped_for_stub() -> None:
    request = MessageRequest(message="create TODO.md hi", mode="default", workspace="/repo", language="zh-CN")
    summary = final_summary_text(
        request,
        [
            {
                "tool": "apply_patch",
                "success": True,
                "status": "applied",
                "operation": "create",
                "files": ["TODO.md"],
                "verification": {"status": "skipped", "reason": "no test command detected"},
            }
        ],
        "Runtime 骨架已连接。",
        "stub",
    )

    assert "已执行 `create`，文件: TODO.md。" in summary
    assert "验证跳过: no test command detected。" in summary


def test_patch_final_reports_verification_denied_for_stub() -> None:
    request = MessageRequest(message="create TODO.md hi", mode="default", workspace="/repo", language="zh-CN")
    summary = final_summary_text(
        request,
        [
            {
                "tool": "apply_patch",
                "success": True,
                "status": "applied",
                "operation": "create",
                "files": ["TODO.md"],
                "verification": {"status": "denied", "command": "rm -rf build", "reason": "禁止执行高风险命令: rm"},
            }
        ],
        "Runtime 骨架已连接。",
        "stub",
    )

    assert "已执行 `create`，文件: TODO.md。" in summary
    assert "验证未运行: 禁止执行高风险命令: rm。" in summary


def test_patch_final_reports_verification_failure_analysis_for_stub() -> None:
    request = MessageRequest(message="replace sample.txt old => new", mode="default", workspace="/repo", language="zh-CN")
    summary = final_summary_text(
        request,
        [
            {
                "tool": "apply_patch",
                "success": True,
                "status": "applied",
                "operation": "replace",
                "files": ["sample.txt"],
                "verification": {
                    "status": "failed",
                    "command": "python3 -m pytest",
                    "analysis": {"summary": "1 failed, 2 passed in 0.12s", "failures": []},
                },
            }
        ],
        "Runtime 骨架已连接。",
        "stub",
    )

    assert "验证失败: `python3 -m pytest`。 失败摘要: 1 failed, 2 passed in 0.12s" in summary


def test_patch_final_reports_rejected_for_stub() -> None:
    request = MessageRequest(message="replace sample.txt old => new", mode="default", workspace="/repo", language="zh-CN")
    summary = final_summary_text(
        request,
        [{"tool": "apply_patch", "success": False, "status": "rejected", "operation": "replace", "files": ["sample.txt"]}],
        "Runtime 骨架已连接。",
        "stub",
    )

    assert "用户拒绝应用 patch，文件: sample.txt。" in summary


@pytest.mark.asyncio
async def test_review_rules_endpoint_uses_workspace_config(tmp_path: Path) -> None:
    config_dir = tmp_path / ".aicode"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        '{"review":{"disabledRules":["large_diff","old_rule"],"largeDiffThreshold":1200,"maxFindings":25}}',
        encoding="utf-8",
    )

    data = await review_rules(str(tmp_path))
    rules = {rule["id"]: rule for rule in data["rules"]}

    assert data["effective_config"]["large_diff_threshold"] == 1200
    assert data["effective_config"]["max_findings"] == 25
    assert rules["large_diff"]["enabled"] is False
    assert data["config_warnings"][0]["rule"] == "old_rule"


@pytest.mark.asyncio
async def test_model_routes_endpoint_returns_route_status() -> None:
    data = await model_routes()

    assert data["provider"]["primary"] == "openai_compatible"
    assert data["provider"]["fallback"] == "stub"
    assert "reviewer" in data["routes"]
    assert "summarizer" in data["routes"]
    assert "api_key_env" in data["openai_compatible"]


@pytest.mark.asyncio
async def test_execute_tool_waits_for_medium_shell_approval(tmp_path: Path) -> None:
    audit.path = tmp_path / "audit.jsonl"
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="shell python3 -c 'print(123)'", mode="default", workspace=str(tmp_path), language="zh-CN")

    task = asyncio.create_task(execute_tool(session, request, "run_shell", {"command": "python3 -c 'print(123)'"}))
    started = await asyncio.wait_for(session.events.get(), timeout=1)
    approval = await asyncio.wait_for(session.events.get(), timeout=1)

    assert started["type"] == "tool.started"
    assert approval["type"] == "approval.requested"
    assert approval["kind"] == "tool"
    assert approval["tool"] == "run_shell"
    assert session.resolve_approval(approval["approval_id"], accepted=True)

    result = await asyncio.wait_for(task, timeout=5)
    output = await asyncio.wait_for(session.events.get(), timeout=1)

    assert result.success
    assert "123" in result.text
    assert output["type"] == "tool.output"
    assert output["data"]["approved"] is True


@pytest.mark.asyncio
async def test_execute_tool_reports_rejected_medium_shell(tmp_path: Path) -> None:
    audit.path = tmp_path / "audit.jsonl"
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="shell python3 -c 'print(123)'", mode="default", workspace=str(tmp_path), language="zh-CN")

    task = asyncio.create_task(execute_tool(session, request, "run_shell", {"command": "python3 -c 'print(123)'"}))
    await asyncio.wait_for(session.events.get(), timeout=1)
    approval = await asyncio.wait_for(session.events.get(), timeout=1)
    assert session.resolve_approval(approval["approval_id"], accepted=False)

    result = await asyncio.wait_for(task, timeout=5)
    rejected = await asyncio.wait_for(session.events.get(), timeout=1)

    assert not result.success
    assert "拒绝" in result.error
    assert rejected["type"] == "tool.rejected"


@pytest.mark.asyncio
async def test_post_patch_verification_skips_when_no_test_command(tmp_path: Path) -> None:
    audit.path = tmp_path / "audit.jsonl"
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="create TODO.md hi", mode="default", workspace=str(tmp_path), language="zh-CN")

    result = await run_post_patch_verification(session, request)
    skipped = await asyncio.wait_for(session.events.get(), timeout=1)

    assert result["status"] == "skipped"
    assert skipped["type"] == "verification.skipped"


@pytest.mark.asyncio
async def test_post_patch_verification_runs_detected_tests(tmp_path: Path) -> None:
    audit.path = tmp_path / "audit.jsonl"
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="replace x", mode="default", workspace=str(tmp_path), language="zh-CN")

    result = await run_post_patch_verification(session, request)
    started = await asyncio.wait_for(session.events.get(), timeout=1)
    tool_started = await asyncio.wait_for(session.events.get(), timeout=1)
    tool_output = await asyncio.wait_for(session.events.get(), timeout=5)
    completed = await asyncio.wait_for(session.events.get(), timeout=1)

    assert result["status"] == "passed"
    assert result["command"] == "python3 -m pytest"
    assert started["type"] == "verification.started"
    assert tool_started["type"] == "tool.started"
    assert tool_output["type"] == "tool.output"
    assert completed["type"] == "verification.completed"
    assert completed["success"] is True


@pytest.mark.asyncio
async def test_post_patch_verification_emits_failure_analysis(tmp_path: Path) -> None:
    audit.path = tmp_path / "audit.jsonl"
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fail.py").write_text("def test_fail():\n    assert False\n", encoding="utf-8")
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="replace x", mode="default", workspace=str(tmp_path), language="zh-CN")

    result = await run_post_patch_verification(session, request)
    events = []
    while not session.events.empty():
        events.append(await session.events.get())

    analysis = next(event for event in events if event["type"] == "verification.analysis")
    completed = next(event for event in events if event["type"] == "verification.completed")

    assert result["status"] == "failed"
    assert result["analysis"]["framework"] == "pytest"
    assert result["analysis"]["failure_count"] >= 1
    assert "failed" in result["analysis"]["summary"]
    assert analysis["analysis"]["framework"] == "pytest"
    assert completed["success"] is False


@pytest.mark.asyncio
async def test_post_patch_verification_denies_unsafe_configured_command(tmp_path: Path) -> None:
    audit.path = tmp_path / "audit.jsonl"
    config_dir = tmp_path / ".aicode"
    config_dir.mkdir()
    (config_dir / "config.json").write_text('{"commands":{"test":"rm -rf build"}}', encoding="utf-8")
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="replace x", mode="default", workspace=str(tmp_path), language="zh-CN")

    result = await run_post_patch_verification(session, request)
    denied = await asyncio.wait_for(session.events.get(), timeout=1)

    assert result["status"] == "denied"
    assert result["command"] == "rm -rf build"
    assert result["risk_level"] == "high"
    assert "高风险命令" in result["reason"]
    assert denied["type"] == "verification.denied"
    assert denied["command"] == "rm -rf build"
    assert denied["risk_level"] == "high"
    assert session.events.empty()
