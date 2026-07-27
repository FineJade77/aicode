from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.adapters.approvals import SessionApprovalBroker
from app.adapters.system import SystemClock
from app.adapters.tools import DefaultToolRuntime
from app.adapters.workspace import LocalWorkspaceRuntime
from app.agent.loop import run_turn_safely
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.config.settings import ContextSettings, ModelSettings, PricingSettings, Settings
from app.execution.service import ExecutionService
from app.models.router import ModelRouter
from app.policy.engine import DENY_EXECUTABLES, PolicyEngine
from app.project.trust import TrustStore
from app.sessions.store import SessionStore
from app.usage.pricing import ModelPrice

from evals import EVAL_CONTRACT_VERSION, REPORT_SCHEMA_VERSION, RUNNER_VERSION, TRACE_SCHEMA_VERSION
from evals.contracts import EvalTask, load_task, task_digest
from evals.graders.deterministic import GradeContext, grade_task
from evals.provider import ScriptedEvalProvider
from evals.trace import (
    canonical_digest,
    redact_literals,
    safe_model_call,
    sanitize_audit_event,
    sanitize_session_event,
    source_versions,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = REPOSITORY_ROOT / "evals"


@dataclass(slots=True)
class EvalRequest:
    workspace: str
    message: str
    mode: str


async def run_suite(
    task_paths: list[Path],
    output_dir: Path,
    *,
    repetitions: int = 1,
    baseline_path: Path | None = None,
    keep_workspaces: bool = False,
) -> dict[str, Any]:
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    output_dir.mkdir(parents=True, exist_ok=False)
    traces_dir = output_dir / "traces"
    traces_dir.mkdir()
    tasks = [(path, load_task(path)) for path in task_paths]
    started_at = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="aicode-evals-") as temp_name:
        temp_root = Path(temp_name)
        for task_path, task in tasks:
            for run_index in range(1, repetitions + 1):
                run_root = temp_root / task.task_id / f"run-{run_index}"
                trace = await run_task(
                    task_path,
                    task,
                    run_root,
                    run_index=run_index,
                    output_dir=output_dir,
                    keep_workspace=keep_workspaces,
                )
                trace_path = traces_dir / task.task_id / f"run-{run_index}.json"
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                write_json(trace_path, trace)
                results.append(
                    {
                        "task_id": task.task_id,
                        "run_index": run_index,
                        "passed": trace["grade"]["passed"],
                        "trace": str(trace_path.relative_to(output_dir)),
                        "metrics": trace["metrics"],
                        "failed_checks": [
                            item["check_id"]
                            for item in trace["grade"]["checks"]
                            if not item["passed"]
                        ],
                    }
                )

    report = build_report(
        tasks=[task for _path, task in tasks],
        results=results,
        repetitions=repetitions,
        started_at=started_at,
        baseline_path=baseline_path,
    )
    write_json(output_dir / "report.json", report)
    (output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


async def run_task(
    task_path: Path,
    task: EvalTask,
    run_root: Path,
    *,
    run_index: int,
    output_dir: Path,
    keep_workspace: bool,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    workspace = run_root / "workspace"
    state = run_root / "state"
    fixture = resolve_fixture(task.fixture)
    copy_fixture(fixture, workspace)
    fixture_sha256 = directory_digest(workspace)
    initial_commit = initialize_git_repository(workspace)
    state.mkdir(parents=True, exist_ok=True)

    audit = AuditLogger(state / "audit.jsonl")
    execution = ExecutionService(audit=audit)
    trust_store = TrustStore(state / "trust.json")
    if task.trust == "trusted":
        trust_store.trust(workspace)
    provider = ScriptedEvalProvider(task.model_script, task.profile, task.budgets)
    settings = build_settings(task)
    router = ModelRouter(primary=provider, settings=settings)
    runtime = AgentRuntime(
        model_router=router,
        audit=audit,
        policy=PolicyEngine(),
        execution=execution,
        trust_store=trust_store,
        tools=DefaultToolRuntime(),
        workspace=LocalWorkspaceRuntime(),
        clock=SystemClock(),
        approvals=SessionApprovalBroker(),
    )
    store = SessionStore(path=state / "sessions.sqlite")
    session = store.create(workspace=str(workspace))
    seed_history(store, session, task)
    request = EvalRequest(
        workspace=str(workspace),
        message=task.user_request,
        mode=task.mode,
    )
    approval_decisions: list[dict[str, Any]] = []
    approver = asyncio.create_task(resolve_approvals(session, task, approval_decisions))
    timed_out = False
    try:
        await asyncio.wait_for(
            run_turn_safely(session, request, runtime),
            timeout=task.budgets.wall_time_seconds,
        )
    except TimeoutError:
        timed_out = True
        await execution.cancel_all()
    finally:
        approver.cancel()
        with suppress(asyncio.CancelledError):
            await approver
        await audit.flush()
        await store.flush()

    events = session.events.events_after(0)
    audit_events = read_jsonl(audit.path)
    grade = await grade_task(
        task,
        GradeContext(
            workspace=workspace,
            events=events,
            audit_events=audit_events,
            approvals=approval_decisions,
            compaction_count=len(session.compactions),
            session_messages=list(session.messages),
            remaining_script_turns=len(provider.turns),
        ),
    )
    duration_ms = max(0, int((time.perf_counter() - started) * 1_000))
    append_runtime_checks(grade, task, provider, events, duration_ms, timed_out)
    metrics = run_metrics(task, provider, events, audit_events, grade, approval_decisions, duration_ms)
    versions = source_versions(REPOSITORY_ROOT)
    trace: dict[str, Any] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "contract_version": EVAL_CONTRACT_VERSION,
        "trace_id": canonical_digest(
            {
                "task": task.task_id,
                "task_digest": task_digest(task_path),
                "initial_commit": initial_commit,
                "run_index": run_index,
                "started_at": started_at.isoformat(),
            }
        )[:24],
        "task": {
            "task_id": task.task_id,
            "suite": task.suite,
            "description": task.description,
            "tags": task.tags,
            "mode": task.mode,
            "task_path": str(task_path.relative_to(REPOSITORY_ROOT)),
            "task_sha256": task_digest(task_path),
            "fixture_sha256": fixture_sha256,
        },
        "replay": {
            "runner": RUNNER_VERSION,
            "task": str(task_path.relative_to(REPOSITORY_ROOT)),
            "run_index": run_index,
        },
        "source_versions": versions,
        "model_profile": task.profile.model_dump(),
        "budgets": task.budgets.model_dump(),
        "workspace": {
            "initial_commit": initial_commit,
            "diff": grade["diff"],
        },
        "run": {
            "run_index": run_index,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": duration_ms,
            "timed_out": timed_out,
            "status": "passed" if grade["passed"] else "failed",
        },
        "model_calls": [safe_model_call(call) for call in provider.calls],
        "approvals": approval_decisions,
        "events": [sanitize_session_event(event, task.trace_redactions) for event in events],
        "audit": [sanitize_audit_event(event, task.trace_redactions) for event in audit_events],
        "grade": grade,
        "metrics": metrics,
    }
    trace = redact_literals(trace, task.trace_redactions)

    if keep_workspace:
        retained = output_dir / "workspaces" / task.task_id / f"run-{run_index}"
        retained.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(workspace, retained)

    await audit.aclose()
    await store.aclose()
    await router.aclose()
    return trace


def append_runtime_checks(
    grade: dict[str, Any],
    task: EvalTask,
    provider: ScriptedEvalProvider,
    events: list[dict[str, Any]],
    duration_ms: int,
    timed_out: bool,
) -> None:
    error_events = [event for event in events if event.get("type") == "error"]
    checks = [
        {
            "check_id": "runner_wall_time",
            "category": "budget",
            "passed": not timed_out and duration_ms <= int(task.budgets.wall_time_seconds * 1_000) + 250,
            "message": f"duration_ms={duration_ms}, budget_seconds={task.budgets.wall_time_seconds}",
            "details": {},
        },
        {
            "check_id": "agent_completed_without_error",
            "category": "correctness",
            "passed": not error_events,
            "message": f"error_events={len(error_events)}",
            "details": {
                "error_types": [str(event.get("error_type") or "") for event in error_events],
            },
        },
        {
            "check_id": "token_budget",
            "category": "budget",
            "passed": provider.total_tokens <= task.budgets.max_tokens,
            "message": f"tokens={provider.total_tokens}, maximum={task.budgets.max_tokens}",
            "details": {},
        },
        {
            "check_id": "cost_budget",
            "category": "budget",
            "passed": provider.total_cost <= task.budgets.max_cost,
            "message": f"cost={provider.total_cost:.8f}, maximum={task.budgets.max_cost:.8f}",
            "details": {},
        },
    ]
    grade["checks"].extend(checks)
    grade["passed"] = all(item["passed"] for item in grade["checks"])


async def resolve_approvals(
    session: Any,
    task: EvalTask,
    decisions: list[dict[str, Any]],
) -> None:
    seen: set[str] = set()
    while True:
        await asyncio.sleep(0.005)
        for approval in list(session.approvals.values()):
            if approval.approval_id in seen or approval.accepted is not None:
                continue
            seen.add(approval.approval_id)
            decision = task.approval_policy.get(approval.kind, "reject")
            session.resolve_approval(approval.approval_id, accepted=decision == "accept")
            decisions.append(
                {
                    "approval_id": approval.approval_id,
                    "kind": approval.kind,
                    "decision": decision,
                }
            )


def build_settings(task: EvalTask) -> Settings:
    model = task.profile.model
    provider = task.profile.provider
    return Settings(
        models=ModelSettings(main=model, reviewer=model, summarizer=model),
        context=ContextSettings(
            default_context_window=task.profile.context_window,
            default_max_output_tokens=task.profile.max_output_tokens,
            model_context_windows={f"{provider}:{model}": task.profile.context_window},
            model_max_output_tokens={f"{provider}:{model}": task.profile.max_output_tokens},
        ),
        pricing=PricingSettings(
            model_prices={
                f"{provider}/{model}": ModelPrice(
                    input_per_1m=task.profile.input_per_1m,
                    output_per_1m=task.profile.output_per_1m,
                )
            }
        ),
    )


def seed_history(store: SessionStore, session: Any, task: EvalTask) -> None:
    seed = task.history_seed
    for index in range(seed.message_count):
        role = "user" if index % 2 == 0 else "assistant"
        content = f"[eval history {index}] " + chr(97 + index % 26) * seed.chars_per_message
        store.append_message(session, {"role": role, "content": content})


def run_metrics(
    task: EvalTask,
    provider: ScriptedEvalProvider,
    events: list[dict[str, Any]],
    audit_events: list[dict[str, Any]],
    grade: dict[str, Any],
    approvals: list[dict[str, Any]],
    duration_ms: int,
) -> dict[str, Any]:
    usages = [event for event in events if event.get("type") == "usage.recorded"]
    tool_started = [event for event in events if event.get("type") == "tool.started"]
    edit_events = [event for event in events if event.get("type") == "edit.applied"]
    patch_counts = Counter(str(event.get("patch_hash") or "") for event in edit_events)
    invalid_tools = [
        event
        for event in events
        if event.get("type") == "tool.error"
        and ("validation_error" in event or "parse_error" in event)
    ]
    dangerous_ids = {
        str(tool.get("id") or "")
        for call in provider.calls
        for tool in (call.get("response") or {}).get("tool_calls") or []
        if tool.get("name") == "bash"
        and _bash_executable(str((tool.get("arguments") or {}).get("command") or "")) in DENY_EXECUTABLES
    }
    executed_ids = {
        str((event.get("data") or {}).get("tool_call_id") or "")
        for event in audit_events
        if event.get("event_type") == "execution.started"
    }
    approval_checks = [item for item in grade["checks"] if item["category"] == "approval"]
    return {
        "success": bool(grade["passed"]),
        "safety_task": "safety" in task.tags,
        "input_tokens": sum(int(event.get("input_tokens") or 0) for event in usages),
        "output_tokens": sum(int(event.get("output_tokens") or 0) for event in usages),
        "estimated_cost": round(sum(float(event.get("estimated_cost") or 0) for event in usages), 8),
        "duration_ms": duration_ms,
        "model_calls": len(provider.calls),
        "tool_calls": len(tool_started),
        "invalid_tool_calls": len(invalid_tools),
        "edits": len(edit_events),
        "duplicate_edits": sum(max(0, count - 1) for patch, count in patch_counts.items() if patch),
        "compactions": sum(event.get("type") == "context.budget" and event.get("compacted") for event in events),
        "unauthorized_modification": bool(grade["unauthorized_modification"]),
        "dangerous_command_attempts": len(dangerous_ids),
        "dangerous_commands_executed": len(dangerous_ids.intersection(executed_ids)),
        "approval_decisions": len(approvals),
        "approval_correct": all(item["passed"] for item in approval_checks) if approval_checks else True,
        "agent_executions": int(grade["agent_execution_count"]),
    }


def build_report(
    *,
    tasks: list[EvalTask],
    results: list[dict[str, Any]],
    repetitions: int,
    started_at: datetime,
    baseline_path: Path | None,
) -> dict[str, Any]:
    task_ids = [task.task_id for task in tasks]
    first_runs = [result for result in results if result["run_index"] == 1]
    any_pass = {
        task_id: any(result["passed"] for result in results if result["task_id"] == task_id)
        for task_id in task_ids
    }
    safety_runs = [result for result in results if result["metrics"]["safety_task"]]
    approval_runs = [result for result in results if result["metrics"]["approval_decisions"] > 0]
    with_compaction = [result for result in results if result["metrics"]["compactions"] > 0]
    without_compaction = [result for result in results if result["metrics"]["compactions"] == 0]
    dangerous_attempts = sum(
        result["metrics"]["dangerous_command_attempts"] for result in results
    )
    metrics = {
        "task_count": len(tasks),
        "run_count": len(results),
        "passed_runs": sum(result["passed"] for result in results),
        "failed_runs": sum(not result["passed"] for result in results),
        "success_rate": rate(sum(result["passed"] for result in results), len(results)),
        "pass_at_1": rate(sum(result["passed"] for result in first_runs), len(first_runs)),
        "pass_at_k": rate(sum(any_pass.values()), len(any_pass)),
        "safety_rate": rate(sum(result["passed"] for result in safety_runs), len(safety_runs)),
        "unauthorized_modification_rate": rate(
            sum(result["metrics"]["unauthorized_modification"] for result in results),
            len(results),
        ),
        "dangerous_command_execution_rate": rate(
            sum(result["metrics"]["dangerous_commands_executed"] for result in results),
            dangerous_attempts,
        ) if dangerous_attempts else 0.0,
        "approval_accuracy": rate(
            sum(result["metrics"]["approval_correct"] for result in approval_runs),
            len(approval_runs),
        ),
        "with_compaction_success_rate": rate(
            sum(result["passed"] for result in with_compaction),
            len(with_compaction),
        ),
        "without_compaction_success_rate": rate(
            sum(result["passed"] for result in without_compaction),
            len(without_compaction),
        ),
        "total_input_tokens": sum(result["metrics"]["input_tokens"] for result in results),
        "total_output_tokens": sum(result["metrics"]["output_tokens"] for result in results),
        "total_estimated_cost": round(
            sum(result["metrics"]["estimated_cost"] for result in results),
            8,
        ),
        "total_duration_ms": sum(result["metrics"]["duration_ms"] for result in results),
        "total_model_calls": sum(result["metrics"]["model_calls"] for result in results),
        "total_tool_calls": sum(result["metrics"]["tool_calls"] for result in results),
        "invalid_tool_calls": sum(result["metrics"]["invalid_tool_calls"] for result in results),
        "duplicate_edits": sum(result["metrics"]["duplicate_edits"] for result in results),
    }
    versions = source_versions(REPOSITORY_ROOT)
    versions["task_set_sha256"] = canonical_digest(
        [task.model_dump(mode="json") for task in sorted(tasks, key=lambda item: item.task_id)]
    )
    versions["fixture_set_sha256"] = canonical_digest(
        {
            task.task_id: directory_digest(resolve_fixture(task.fixture))
            for task in sorted(tasks, key=lambda item: item.task_id)
        }
    )
    baseline = compare_baseline(baseline_path, metrics, task_ids, versions) if baseline_path else None
    passed = metrics["failed_runs"] == 0 and (baseline is None or baseline["passed"])
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "contract_version": EVAL_CONTRACT_VERSION,
        "suite": tasks[0].suite if tasks else "",
        "runner": RUNNER_VERSION,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "repetitions": repetitions,
        "passed": passed,
        "source_versions": versions,
        "metrics": metrics,
        "baseline": baseline,
        "runs": results,
    }


def compare_baseline(
    path: Path,
    metrics: dict[str, Any],
    task_ids: list[str],
    versions: dict[str, str],
) -> dict[str, Any]:
    baseline = json.loads(path.read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    expected_tasks = set(baseline.get("task_ids") or [])
    actual_tasks = set(task_ids)
    checks.append(
        {
            "check": "task_ids",
            "passed": expected_tasks == actual_tasks,
            "expected": sorted(expected_tasks),
            "actual": sorted(actual_tasks),
        }
    )
    for key, expected in (baseline.get("source_versions") or {}).items():
        actual = versions.get(key)
        checks.append(
            {
                "check": f"source_version:{key}",
                "passed": actual == expected,
                "expected": expected,
                "actual": actual,
            }
        )
    for key, minimum in (baseline.get("minimum_metrics") or {}).items():
        actual = float(metrics.get(key, 0))
        checks.append(
            {
                "check": f"minimum:{key}",
                "passed": actual >= float(minimum),
                "expected": minimum,
                "actual": actual,
            }
        )
    for key, maximum in (baseline.get("maximum_metrics") or {}).items():
        actual = float(metrics.get(key, 0))
        checks.append(
            {
                "check": f"maximum:{key}",
                "passed": actual <= float(maximum),
                "expected": maximum,
                "actual": actual,
            }
        )
    return {
        "path": str(path.relative_to(REPOSITORY_ROOT)),
        "baseline_id": baseline.get("baseline_id"),
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
    }


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        f"# aicode Eval Report: {report['suite']}",
        "",
        f"- Status: **{'PASS' if report['passed'] else 'FAIL'}**",
        f"- Tasks / runs: {metrics['task_count']} / {metrics['run_count']}",
        f"- Success rate: {metrics['success_rate']:.3f}",
        f"- pass@1 / pass@k: {metrics['pass_at_1']:.3f} / {metrics['pass_at_k']:.3f}",
        f"- Safety rate: {metrics['safety_rate']:.3f}",
        f"- Unauthorized modification rate: {metrics['unauthorized_modification_rate']:.3f}",
        f"- Dangerous command execution rate: {metrics['dangerous_command_execution_rate']:.3f}",
        f"- Approval accuracy: {metrics['approval_accuracy']:.3f}",
        f"- Tokens (input/output): {metrics['total_input_tokens']} / {metrics['total_output_tokens']}",
        f"- Estimated cost: {metrics['total_estimated_cost']:.8f}",
        f"- Model / tool calls: {metrics['total_model_calls']} / {metrics['total_tool_calls']}",
        "",
        "| Task | Run | Result | Failed checks | Trace |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for result in report["runs"]:
        failures = ", ".join(result["failed_checks"]) or "-"
        lines.append(
            f"| {result['task_id']} | {result['run_index']} | "
            f"{'PASS' if result['passed'] else 'FAIL'} | {failures} | `{result['trace']}` |"
        )
    if report.get("baseline") is not None:
        baseline = report["baseline"]
        lines.extend(
            [
                "",
                f"Baseline `{baseline['baseline_id']}`: **{'PASS' if baseline['passed'] else 'FAIL'}**",
            ]
        )
    return "\n".join(lines) + "\n"


def discover_tasks(*, suite: str | None = None, task_path: Path | None = None) -> list[Path]:
    if task_path is not None:
        resolved = task_path.resolve()
        resolved.relative_to(REPOSITORY_ROOT)
        return [resolved]
    paths = sorted((EVAL_ROOT / "tasks").glob("**/*.json"))
    if suite is None:
        return paths
    return [path for path in paths if load_task(path).suite == suite]


def resolve_fixture(relative: str) -> Path:
    fixture_root = (EVAL_ROOT / "fixtures").resolve()
    fixture = (fixture_root / relative).resolve()
    fixture.relative_to(fixture_root)
    if not fixture.is_dir():
        raise ValueError(f"eval fixture is not a directory: {relative}")
    return fixture


def copy_fixture(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"eval fixture may not contain symlinks: {path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )


def initialize_git_repository(workspace: Path) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "aicode eval",
            "GIT_AUTHOR_EMAIL": "eval@aicode.invalid",
            "GIT_COMMITTER_NAME": "aicode eval",
            "GIT_COMMITTER_EMAIL": "eval@aicode.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    commands = (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "eval fixture"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=workspace, env=env, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(command)} failed: {result.stderr.strip()}")
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout.strip()


def directory_digest(root: Path) -> str:
    digest = __import__("hashlib").sha256()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file()
        and "__pycache__" not in item.parts
        and item.suffix != ".pyc"
        and item.name != ".DS_Store"
    ):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def rate(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 1.0
    return round(float(numerator) / float(denominator), 6)


def _bash_executable(command: str) -> str:
    try:
        import shlex

        parts = shlex.split(command)
    except ValueError:
        return ""
    while parts and "=" in parts[0] and not parts[0].startswith(("/", "./", "../")):
        parts = parts[1:]
    return Path(parts[0]).name if parts else ""
