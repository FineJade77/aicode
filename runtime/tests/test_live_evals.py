"""The live eval mode.

The scripted suite proves the Agent Loop replays a script correctly. That is a
different question from whether the Agent can finish a real task, which is what
the live suite measures. These tests cover the harness that makes the second
question askable — accounting, budgets, grading and attribution — without ever
calling a real model.
"""

from __future__ import annotations

import json
import pathlib
import re
from collections import Counter
from pathlib import Path

import pytest

from app.models.provider import CompletionRequest, StreamEvent, ToolCallRequest, Usage
from evals import reference_solutions
from evals.contracts import EvalTask, load_task
from evals.provider import EvalBudgetExceeded, LiveEvalProvider
from evals.runner.core import (
    FAILURE_BUDGET,
    FAILURE_EDIT,
    FAILURE_ERROR,
    FAILURE_LOCALIZATION,
    FAILURE_SAFETY,
    FAILURE_VERIFICATION,
    REPOSITORY_ROOT,
    build_provider,
    build_settings,
    category_breakdown,
    discover_tasks,
    failure_reason,
    override_live_profile,
    percentile,
    preflight_live_tasks,
    seeded_chars_per_message,
    task_category,
)

EVAL_ROOT = REPOSITORY_ROOT / "evals"

LIVE_PROFILE = {
    "provider": "anthropic",
    "model": "claude-sonnet-5",
    "context_window": 200000,
    "max_output_tokens": 8192,
    "input_per_1m": 3.0,
    "output_per_1m": 15.0,
}
BUDGETS = {"max_model_calls": 5, "max_tokens": 10_000, "max_cost": 1.0, "wall_time_seconds": 60}


def live_task(**overrides) -> EvalTask:
    payload = {
        "contract_version": "1.0",
        "task_id": "example_task",
        "suite": "live",
        "description": "example",
        "fixture": "single_file_fix",
        "user_request": "fix it",
        "provider_mode": "live",
        "profile": LIVE_PROFILE,
        "budgets": BUDGETS,
        "checks": {},
    }
    payload.update(overrides)
    return EvalTask.model_validate(payload)


class FakeProvider:
    """Stands in for a real provider: a scripted stream, no network."""

    provider_name = "anthropic"

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[CompletionRequest] = []
        self.closed = False
        self.configured = True

    def is_configured(self) -> bool:
        return self.configured

    async def stream_complete(self, request: CompletionRequest):
        self.requests.append(request)
        text, tools, usage = self.responses.pop(0)
        if text:
            yield StreamEvent(type="text_delta", text=text)
        for name, arguments in tools:
            yield StreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(id=f"call_{name}", name=name, arguments=arguments),
            )
        yield StreamEvent(type="done", usage=usage, model=request.model)

    async def aclose(self) -> None:
        self.closed = True


async def drain(provider: LiveEvalProvider, purpose: str = "main") -> list[StreamEvent]:
    request = CompletionRequest(purpose=purpose, system="s", messages=[], model="ignored")
    return [event async for event in provider.stream_complete(request)]


# --- contracts --------------------------------------------------------------


def test_a_live_task_may_not_carry_a_script():
    """A script a live run silently ignores reads as an assertion that never runs."""
    with pytest.raises(ValueError):
        live_task(model_script=[{"purpose": "main", "text": "hi"}])


def test_a_live_task_may_not_use_the_scripted_provider():
    with pytest.raises(ValueError):
        live_task(profile={**LIVE_PROFILE, "provider": "scripted"})


def test_a_scripted_task_still_requires_a_script():
    with pytest.raises(ValueError):
        EvalTask.model_validate(
            {
                "contract_version": "1.0",
                "task_id": "scripted_task",
                "suite": "smoke",
                "description": "d",
                "fixture": "single_file_fix",
                "user_request": "u",
                "profile": {**LIVE_PROFILE, "provider": "scripted"},
                "budgets": BUDGETS,
                "checks": {},
            }
        )


# --- accounting parity ------------------------------------------------------


@pytest.mark.asyncio
async def test_live_accounting_matches_the_scripted_surface():
    """`run_metrics` reads these three names off either provider, unbranched."""
    inner = FakeProvider(("done", [("bash", {"command": "ls"})], Usage(input_tokens=1000, output_tokens=200)))
    provider = LiveEvalProvider(inner, live_task().profile, live_task().budgets)

    await drain(provider)

    assert provider.total_tokens == 1200
    # 1000 in at $3/1M plus 200 out at $15/1M.
    assert provider.total_cost == pytest.approx((1000 * 3.0 + 200 * 15.0) / 1_000_000)
    call = provider.calls[0]
    assert call["status"] == "completed"
    assert call["response"]["tool_calls"] == [{"id": "call_bash", "name": "bash", "arguments": {"command": "ls"}}]
    assert call["response"]["input_tokens"] == 1000


@pytest.mark.asyncio
async def test_the_task_model_overrides_the_route():
    """`live_model` must not be bypassable by a purpose-specific route."""
    inner = FakeProvider(("", [], Usage()))
    provider = LiveEvalProvider(inner, live_task().profile, live_task().budgets, model="claude-opus-5")

    await drain(provider, purpose="summarizer")

    assert inner.requests[0].model == "claude-opus-5"


@pytest.mark.asyncio
async def test_the_token_budget_stops_the_run():
    """A live overrun spends real money, so the ceiling has to stop it."""
    task = live_task(budgets={**BUDGETS, "max_tokens": 100})
    inner = FakeProvider(("", [], Usage(input_tokens=500, output_tokens=10)))
    provider = LiveEvalProvider(inner, task.profile, task.budgets)

    with pytest.raises(EvalBudgetExceeded):
        await drain(provider)
    assert provider.calls[0]["error_type"] == "EvalBudgetExceeded"


@pytest.mark.asyncio
async def test_the_call_budget_stops_the_run_before_the_request():
    task = live_task(budgets={**BUDGETS, "max_model_calls": 1})
    inner = FakeProvider(("", [], Usage()), ("", [], Usage()))
    provider = LiveEvalProvider(inner, task.profile, task.budgets)

    await drain(provider)
    with pytest.raises(EvalBudgetExceeded):
        await drain(provider)
    # The second call never reached the provider, which is the point: the budget
    # bounds spend rather than describing it afterwards.
    assert len(inner.requests) == 1


@pytest.mark.asyncio
async def test_closing_the_provider_closes_the_real_one():
    inner = FakeProvider()
    await LiveEvalProvider(inner, live_task().profile, live_task().budgets).aclose()
    assert inner.closed


# --- settings ---------------------------------------------------------------


def test_live_settings_keep_ambient_credentials_but_task_pricing(monkeypatch):
    monkeypatch.setenv("AICODE_ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("AICODE_MODEL_MAIN", "some-other-model")

    settings = build_settings(live_task(live_model="claude-opus-5"))

    assert settings.anthropic.base_url == "https://example.invalid"
    # The ambient route must not win: a report's cost column has to describe the
    # model the task pinned.
    assert settings.models.main == "claude-opus-5"
    assert settings.provider.type == "anthropic"
    assert "anthropic/claude-opus-5" in settings.pricing.model_prices


def test_scripted_settings_are_unaffected_by_the_environment(monkeypatch):
    monkeypatch.setenv("AICODE_MODEL_MAIN", "some-other-model")
    task = load_task(EVAL_ROOT / "tasks" / "smoke" / "single_file_fix.json")

    assert build_settings(task).models.main == task.profile.model


def test_build_provider_picks_the_mode(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    task = live_task()
    assert isinstance(build_provider(task, build_settings(task)), LiveEvalProvider)

    scripted = load_task(EVAL_ROOT / "tasks" / "smoke" / "single_file_fix.json")
    assert not isinstance(build_provider(scripted, build_settings(scripted)), LiveEvalProvider)


def test_the_profile_override_retargets_provider_window_and_price_together():
    """Retargeting the provider alone would misreport compaction and cost."""
    task = live_task()
    override = {
        "provider": "openai_compatible",
        "model": "deepseek-chat",
        "context_window": 65536,
        "input_per_1m": 0.28,
        "output_per_1m": 0.42,
    }

    retargeted = override_live_profile(task, override, None)
    settings = build_settings(retargeted)

    assert retargeted.profile.provider == "openai_compatible"
    assert settings.provider.type == "openai_compatible"
    # The window the harness plans compaction against must be the real one.
    assert settings.context.model_context_windows["openai_compatible:deepseek-chat"] == 65536
    price = settings.pricing.model_prices["openai_compatible/deepseek-chat"]
    assert (price.input_per_1m, price.output_per_1m) == (0.28, 0.42)


def test_the_profile_override_is_validated_not_patched():
    """A bad override must fail here, not part-way through a paid run."""
    with pytest.raises(ValueError):
        override_live_profile(live_task(), {"provider": "not_a_provider"}, None)


def test_live_model_wins_over_a_model_inside_the_profile_override():
    task = override_live_profile(live_task(), {"model": "deepseek-chat"}, "deepseek-reasoner")
    assert task.live_model == "deepseek-reasoner"
    assert build_settings(task).models.main == "deepseek-reasoner"


def test_a_profile_override_alone_still_selects_the_model():
    task = override_live_profile(live_task(), {"model": "deepseek-chat"}, None)
    assert build_settings(task).models.main == "deepseek-chat"


def test_the_override_leaves_scripted_tasks_alone():
    """Rewriting a scripted task's profile would void its recorded baseline."""
    scripted = load_task(EVAL_ROOT / "tasks" / "smoke" / "single_file_fix.json")
    assert not scripted.is_live


@pytest.mark.asyncio
async def test_preflight_refuses_before_spending_anything(monkeypatch):
    """Discovering a missing key on task 12 of 28 has already cost money."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="require a configured model provider"):
        await preflight_live_tasks([live_task()])


@pytest.mark.asyncio
async def test_preflight_ignores_scripted_tasks(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    await preflight_live_tasks([load_task(EVAL_ROOT / "tasks" / "smoke" / "single_file_fix.json")])


# --- failure attribution ----------------------------------------------------


def grade(passed: bool, failed_checks: tuple[str, ...] = (), changed: tuple[str, ...] = ()):
    return {
        "passed": passed,
        "checks": [{"check_id": name, "passed": False} for name in failed_checks],
        "changed_paths": list(changed),
    }


def metrics(**overrides):
    base = {
        "unauthorized_modification": False,
        "dangerous_commands_executed": 0,
        "agent_executions": 1,
    }
    base.update(overrides)
    return base


def attribution_task(**overrides) -> EvalTask:
    checks = {
        "test_commands": [["python3", "-m", "pytest", "-q"]],
        "files": [{"path": "calc.py"}],
        "allowed_changed_paths": ["calc.py"],
    }
    checks.update(overrides.pop("checks", {}))
    return live_task(checks=checks, **overrides)


def test_a_passing_run_has_no_failure_reason():
    assert failure_reason(attribution_task(), grade(True), [], metrics(), False) is None


def test_safety_outranks_everything_else():
    reason = failure_reason(
        attribution_task(),
        grade(False, ("test_command_1",)),
        [],
        metrics(unauthorized_modification=True),
        False,
    )
    assert reason == FAILURE_SAFETY


def test_a_timeout_is_a_budget_failure():
    assert failure_reason(attribution_task(), grade(False), [], metrics(), True) == FAILURE_BUDGET


def test_a_budget_error_event_is_a_budget_failure():
    events = [{"type": "error", "error_type": "EvalBudgetExceeded"}]
    assert failure_reason(attribution_task(), grade(False), events, metrics(), False) == FAILURE_BUDGET


def test_a_provider_error_is_its_own_category():
    events = [{"type": "error", "error_type": "ProviderError"}]
    assert failure_reason(attribution_task(), grade(False), events, metrics(), False) == FAILURE_ERROR


def test_touching_nothing_expected_is_a_localization_failure():
    reason = failure_reason(
        attribution_task(),
        grade(False, ("test_command_1",), changed=("notes.md",)),
        [],
        metrics(),
        False,
    )
    assert reason == FAILURE_LOCALIZATION


def test_editing_without_ever_running_a_command_is_a_verification_failure():
    reason = failure_reason(
        attribution_task(),
        grade(False, ("test_command_1",), changed=("calc.py",)),
        [],
        metrics(agent_executions=0),
        False,
    )
    assert reason == FAILURE_VERIFICATION


def test_a_read_only_task_that_changed_nothing_is_not_a_localization_failure():
    """`checks.files` also carries invariants, not just targets.

    A safety task asserting "README.md still says X" changed nothing on purpose;
    reading that as "never found the file" mislabels the one category that is
    supposed to tell you where to look next.
    """
    task = attribution_task(
        checks={"files": [{"path": "README.md"}], "allowed_changed_paths": []}
    )
    reason = failure_reason(task, grade(False, ("event_required:context.budget",)), [], metrics(), False)
    assert reason != FAILURE_LOCALIZATION


def test_a_wrong_edit_that_was_verified_is_an_edit_failure():
    reason = failure_reason(
        attribution_task(),
        grade(False, ("test_command_1",), changed=("calc.py",)),
        [],
        metrics(agent_executions=2),
        False,
    )
    assert reason == FAILURE_EDIT


def test_a_toothless_test_is_an_edit_failure():
    """Suite green, mutation survived: the Agent wrote a test that proves nothing."""
    reason = failure_reason(
        attribution_task(),
        grade(False, ("mutation_1:stats.py",), changed=("calc.py",)),
        [],
        metrics(),
        False,
    )
    assert reason == FAILURE_EDIT


# --- report aggregation -----------------------------------------------------


def test_percentile_uses_nearest_rank():
    values = list(range(1, 21))
    assert percentile(values, 95) == 19
    assert percentile(values, 100) == 20
    assert percentile([], 95) == 0
    # Nearest-rank returns a value that was actually observed.
    assert percentile([10, 100], 95) in {10, 100}


def run_row(task_id: str, run_index: int, passed: bool, category: str, **extra):
    return {
        "task_id": task_id,
        "run_index": run_index,
        "passed": passed,
        "metrics": {
            "category": category,
            "duration_ms": extra.get("duration_ms", 100),
            "estimated_cost": extra.get("estimated_cost", 0.01),
            "failure_reason": extra.get("failure_reason"),
        },
    }


def test_category_breakdown_separates_pass_at_1_from_pass_at_k():
    tasks = [
        live_task(task_id="task_a", tags=["single_file_fix"]),
        live_task(task_id="task_b", tags=["cross_file"]),
    ]
    results = [
        run_row("task_a", 1, False, "single_file_fix", failure_reason=FAILURE_EDIT),
        run_row("task_a", 2, True, "single_file_fix"),
        run_row("task_b", 1, True, "cross_file"),
        run_row("task_b", 2, True, "cross_file"),
    ]

    breakdown = category_breakdown(tasks, results)

    assert breakdown["single_file_fix"]["pass_at_1"] == 0.0
    assert breakdown["single_file_fix"]["pass_at_k"] == 1.0
    assert breakdown["single_file_fix"]["failure_attribution"] == {FAILURE_EDIT: 1}
    assert breakdown["cross_file"]["pass_at_1"] == 1.0


def test_task_category_reads_the_declared_tag():
    assert task_category(live_task(tags=["edit", "cross_file"])) == "cross_file"
    assert task_category(live_task(tags=["edit"])) == "uncategorized"


# --- the task set itself ----------------------------------------------------


def live_tasks() -> list[EvalTask]:
    return [load_task(path) for path in discover_tasks(suite="live")]


def test_the_live_suite_has_the_designed_shape():
    tasks = live_tasks()
    counts = Counter(task_category(task) for task in tasks)

    assert counts == {
        "single_file_fix": 8,
        "cross_file": 6,
        "new_tests": 6,
        "retry_fix": 4,
        "safety": 4,
    }
    assert len({task.task_id for task in tasks}) == len(tasks)
    assert all(task.is_live for task in tasks)
    assert all((EVAL_ROOT / "fixtures" / task.fixture).is_dir() for task in tasks)


def test_no_live_task_lets_the_agent_edit_its_own_tests():
    """Rewriting the test to match a broken fix is the cheapest way to fake a pass."""
    for task in live_tasks():
        allowed = set(task.checks.allowed_changed_paths)
        test_files = {path for path in allowed if Path(path).name.startswith("test_")}
        if task_category(task) == "new_tests":
            # These tasks are *about* writing tests, so the implementation is
            # what must stay frozen instead.
            assert task.checks.forbidden_changed_paths
            continue
        assert not test_files, f"{task.task_id} may edit {test_files}"


@pytest.mark.parametrize("window", [8192, 65536, 131072, 200000])
def test_the_compaction_task_forces_a_compaction_at_every_window(window: int):
    """A seed sized in characters only means something relative to the window.

    Sized for 8k it compacts there and does nothing at 200k, which turns the
    assertion into one that can never fire — the task then reports a model
    failure that is really an authoring bug.
    """
    task = load_task(EVAL_ROOT / "tasks" / "live" / "live_safety_long_context.json")
    retargeted = task.model_copy(
        update={"profile": task.profile.model_copy(update={"context_window": window})}
    )
    settings = build_settings(retargeted)

    chars = seeded_chars_per_message(retargeted, settings)
    seeded_tokens = chars * task.history_seed.message_count / settings.context.chars_per_token

    assert seeded_tokens > window * settings.context.compact_threshold


def test_the_compaction_task_still_asserts_a_compaction():
    task = load_task(EVAL_ROOT / "tasks" / "live" / "live_safety_long_context.json")
    assert task.checks.minimum_compactions >= 1
    assert task.history_seed.target_context_ratio > 0


def test_work_tasks_can_run_commands_and_safety_tasks_cannot():
    """The approval split is the difference between two things being measured.

    A work task that cannot run a command never verifies its own fix, so
    `retry_fix` stops measuring "reads the failure and iterates" and
    `verification_failure` can never fire — the grader ends up doing the
    verification the Agent was supposed to do. A safety task is the opposite:
    refusal is the behaviour under test, so it rejects everything.

    The policy engine still denies destructive executables and protected paths
    outright, so "accept" widens what may be approved, not what may be run.
    """
    for task in live_tasks():
        policy = task.approval_policy
        if task_category(task) == "safety":
            assert policy == {"edit": "reject", "tool": "reject"}, task.task_id
        else:
            assert policy == {"edit": "accept", "tool": "accept"}, task.task_id


def test_new_test_tasks_carry_a_mutation():
    """Without one, an empty test file passes the suite and scores as success."""
    for task in live_tasks():
        if task_category(task) == "new_tests":
            assert task.checks.mutations, f"{task.task_id} has no mutation"


def test_the_live_task_set_does_not_leak_answers_into_the_fixtures():
    for task in live_tasks():
        for mutation in task.checks.mutations:
            fixture_files = (EVAL_ROOT / "fixtures" / task.fixture).rglob("*")
            blob = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in fixture_files
                if path.is_file() and path.suffix in {".py", ".md", ".sh"}
            )
            assert mutation.new_text not in blob or not mutation.new_text.strip()


@pytest.mark.parametrize(
    "task",
    [pytest.param(task, id=task.task_id) for task in live_tasks() if task.checks.test_commands],
)
def test_every_live_task_is_solvable(task: EvalTask):
    """A task no correct edit can satisfy scores a model failure that is not one.

    This is the difference between measuring the Agent and measuring the task
    author, so the reference solution is checked rather than assumed. It also
    re-verifies that each declared mutation is caught once the tests are right,
    which is what stops a `new_tests` task from being passable by an empty file.
    """
    assert reference_solutions.verify(task) == []


def test_the_live_tasks_validate_against_the_task_schema():
    schema = json.loads(
        (REPOSITORY_ROOT / "schemas" / "eval-task.schema.json").read_text(encoding="utf-8")
    )
    enum = schema["properties"]["profile"]["properties"]["provider"]["enum"]
    assert "anthropic" in enum
    assert "model_script" not in schema["required"]
    assert set(schema["required"]) == {
        name for name, field in EvalTask.model_fields.items() if field.is_required()
    }


# --- the harder tier ----------------------------------------------------------


def hard_tasks() -> list[EvalTask]:
    return [load_task(path) for path in discover_tasks(suite="live_hard")]


def test_the_hard_suite_covers_every_difficulty_mechanism():
    """Categories name *why* a task is hard, not what shape it is.

    That is what makes a per-category pass rate answer the question the gated
    roadmap tasks are waiting on — which kind of difficulty defeats the Agent.
    """
    counts = Counter(task_category(task) for task in hard_tasks())

    assert set(counts) == {
        "localization",
        "cross_module",
        "algorithmic",
        "reproduce_first",
        "underspecified",
    }
    assert sum(counts.values()) == 8


def test_hard_tasks_are_live_and_forbid_editing_their_tests():
    for task in hard_tasks():
        assert task.is_live, task.task_id
        assert task.checks.forbidden_changed_paths, task.task_id
        allowed = {Path(path).name for path in task.checks.allowed_changed_paths}
        assert not any(name.startswith("test_") for name in allowed), task.task_id


def test_the_hard_suite_is_separate_from_the_easy_one():
    """Kept apart on purpose: `live` is a cheap regression floor and a known
    reference point, `live_hard` is the tier that can actually produce failure
    attribution. Merging them would make every run pay for both."""
    assert {task.task_id for task in hard_tasks()}.isdisjoint(
        {task.task_id for task in live_tasks()}
    )


@pytest.mark.parametrize(
    "task", [pytest.param(task, id=task.task_id) for task in hard_tasks()]
)
def test_every_hard_task_is_solvable(task: EvalTask):
    """Hard must mean hard, not impossible.

    A task no correct edit can satisfy scores a model failure that is really an
    authoring bug — and on this tier, where failures are the point, that
    distinction is the whole value of the run.
    """
    assert reference_solutions.verify(task) == []


def test_a_policy_only_failure_is_distinguishable_from_never_solving_it():
    """Both fail. Only one of them is a capability signal.

    A run that solves the task and then edits a forbidden file must fail — but
    recording only the verdict makes it read as "could not do it", which is the
    opposite conclusion when calibrating difficulty.
    """
    tasks = [live_task(task_id="task_a", tags=["localization"])]
    cheated = run_row("task_a", 1, False, "localization", failure_reason=FAILURE_SAFETY)
    cheated["metrics"]["tests_passed"] = True

    breakdown = category_breakdown(tasks, [cheated])

    assert breakdown["localization"]["pass_at_1"] == 0.0
    assert breakdown["localization"]["functional_pass_at_1"] == 1.0


def test_a_genuine_failure_shows_in_both_rates():
    tasks = [live_task(task_id="task_b", tags=["algorithmic"])]
    stuck = run_row("task_b", 1, False, "algorithmic", failure_reason=FAILURE_EDIT)
    stuck["metrics"]["tests_passed"] = False

    breakdown = category_breakdown(tasks, [stuck])

    assert breakdown["algorithmic"]["pass_at_1"] == 0.0
    assert breakdown["algorithmic"]["functional_pass_at_1"] == 0.0


def test_the_agent_toolchain_is_recorded_but_not_gated():
    """Recorded because it changes results; not baselined because it is per-machine.

    The harness pins fixtures, tasks and prompts by digest and then hands Agent
    commands the developer's PATH. Baselining that would fail every other
    machine; leaving it unrecorded makes two disagreeing runs look identical.
    """
    from evals.trace import agent_toolchain, source_versions

    versions = source_versions(REPOSITORY_ROOT)
    assert "agent_toolchain" in versions
    assert "python3=" in agent_toolchain()

    baseline = json.loads(
        (EVAL_ROOT / "baselines" / "deterministic-smoke.v1.json").read_text(encoding="utf-8")
    )
    assert "agent_toolchain" not in baseline["source_versions"]


# --- the scale tier -----------------------------------------------------------


def scale_tasks() -> list[EvalTask]:
    return [load_task(path) for path in discover_tasks(suite="live_scale")]


def test_scale_fixtures_are_actually_large():
    """The tier's whole premise is that reading everything stops being cheap.

    A "scale" task over five files would test nothing the hard tier didn't, so
    the size is asserted rather than assumed.
    """
    for task in scale_tasks():
        modules = list((EVAL_ROOT / "fixtures" / task.fixture).rglob("*.py"))
        assert len(modules) >= 30, f"{task.task_id} has only {len(modules)} modules"


def test_the_planted_defect_does_not_stand_out_by_shape():
    """If the broken module were visibly odd, scale would be irrelevant.

    Checked within the peer group the defect hides in — the package of
    near-identical modules — not across the whole fixture, since a registry that
    lists every peer is legitimately larger and is not where the bug is.
    """
    for task in scale_tasks():
        root = EVAL_ROOT / "fixtures" / task.fixture
        package = max(
            (directory for directory in root.iterdir() if directory.is_dir()),
            key=lambda directory: len(list(directory.glob("*.py"))),
        )
        sizes = sorted(
            len(path.read_text(encoding="utf-8").splitlines())
            for path in package.glob("*.py")
            if path.stat().st_size > 0
        )
        assert len(sizes) >= 25, f"{task.task_id}: peer group is too small to hide in"
        # Widest and narrowest peer within a couple of lines of each other.
        assert sizes[-1] - sizes[0] <= 2, f"{task.task_id}: peers are not uniform"


@pytest.mark.parametrize(
    "task", [pytest.param(task, id=task.task_id) for task in scale_tasks()]
)
def test_every_scale_task_is_solvable(task: EvalTask):
    assert reference_solutions.verify(task) == []


# --- the size curve -----------------------------------------------------------


def curve_tasks() -> list[EvalTask]:
    return [load_task(path) for path in discover_tasks(suite="live_scale_curve")]


def test_the_curve_spans_a_wide_size_ladder():
    """Two nearby sizes cannot separate a flat curve from a rising one."""
    sizes = sorted(
        len(list((EVAL_ROOT / "fixtures" / task.fixture / "handlers").glob("*.py"))) - 1
        for task in curve_tasks()
    )

    assert sizes == [10, 30, 100, 300]


def test_only_size_varies_across_the_curve():
    """Any difference in measured effort must be attributable to size alone.

    Same request, same budgets, same model: if those drifted, the curve would be
    measuring the drift instead.
    """
    tasks = curve_tasks()
    assert len({task.user_request for task in tasks}) == 1
    assert len({task.budgets.model_dump_json() for task in tasks}) == 1
    assert len({task.profile.model_dump_json() for task in tasks}) == 1


def test_no_module_carries_a_token_its_peers_lack():
    """The defect must not be findable by one grep, or the curve measures nothing.

    The first version of this tier planted a `weight` token that appeared in the
    broken handler and nowhere else; the model read the test, grepped it, and
    found the module in one hit at every size — an O(1) lookup that made the
    curve look flat regardless of repository size. A defect has to be a
    *cross-reference* error, invisible to token search, for size to be the thing
    being measured.
    """
    for task in curve_tasks():
        handlers = [
            path
            for path in (EVAL_ROOT / "fixtures" / task.fixture / "handlers").glob("*.py")
            if path.name != "__init__.py"
        ]
        vocabularies = {
            path.name: set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", path.read_text(encoding="utf-8")))
            for path in handlers
        }
        for name, vocabulary in vocabularies.items():
            elsewhere = set().union(
                *(other for key, other in vocabularies.items() if key != name)
            )
            unique = vocabulary - elsewhere
            # Its own module name is inherently unique and is not a giveaway:
            # it identifies the file, it does not mark it as broken.
            unique -= {pathlib.Path(name).stem}
            assert not unique, f"{task.task_id}: {name} carries unique tokens {sorted(unique)}"


def test_the_defect_sits_away_from_both_ends():
    """A bug in the first or last module is findable by habit, not by search."""
    for task in curve_tasks():
        handlers = sorted(
            path for path in (EVAL_ROOT / "fixtures" / task.fixture / "handlers").glob("*.py")
            if path.name != "__init__.py"
        )
        broken = [
            index
            for index, path in enumerate(handlers)
            # The defect is a handler filtering on a name that is not its own.
            if f'!= "{path.stem}"' not in path.read_text(encoding="utf-8")
        ]
        assert len(broken) == 1, task.task_id
        assert 0 < broken[0] < len(handlers) - 1, task.task_id


@pytest.mark.parametrize(
    "task", [pytest.param(task, id=task.task_id) for task in curve_tasks()]
)
def test_every_curve_task_is_solvable(task: EvalTask):
    assert reference_solutions.verify(task) == []
