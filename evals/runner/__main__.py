from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from evals.runner.core import REPOSITORY_ROOT, discover_tasks, run_suite


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run isolated aicode task-level evaluations.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--suite", help="Run every task in the named suite.")
    target.add_argument("--task", type=Path, help="Run one task JSON file.")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output-root", type=Path, default=Path(".artifacts/evals"))
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--keep-workspaces", action="store_true")
    parser.add_argument(
        "--live-model",
        help=(
            "Live tasks only: override the model every task calls. Exists so the same "
            "task set can be run against several models without editing the tasks."
        ),
    )
    parser.add_argument(
        "--live-profile",
        help=(
            "Live tasks only: JSON merged into each task's profile, e.g. "
            '\'{"provider": "openai_compatible", "model": "deepseek-chat", '
            '"context_window": 65536, "input_per_1m": 0.28, "output_per_1m": 0.42}\'. '
            "Provider, context window and price move together because they are not "
            "independent: retargeting the provider alone would keep the original "
            "window and prices, which misreports both compaction and cost."
        ),
    )
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    task_path = args.task
    if task_path is not None and not task_path.is_absolute():
        task_path = REPOSITORY_ROOT / task_path
    tasks = discover_tasks(suite=args.suite, task_path=task_path)
    if not tasks:
        raise SystemExit("no eval tasks matched")
    suite_name = args.suite or tasks[0].stem
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root = args.output_root
    if not output_root.is_absolute():
        output_root = REPOSITORY_ROOT / output_root
    output_dir = output_root / f"{suite_name}-{timestamp}-{uuid4().hex[:6]}"
    baseline = args.baseline
    if baseline is not None and not baseline.is_absolute():
        baseline = REPOSITORY_ROOT / baseline
    live_profile = None
    if args.live_profile:
        try:
            live_profile = json.loads(args.live_profile)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--live-profile is not valid JSON: {exc}") from exc
        if not isinstance(live_profile, dict):
            raise SystemExit("--live-profile must be a JSON object")
    try:
        report = await run_suite(
            tasks,
            output_dir,
            repetitions=args.repetitions,
            baseline_path=baseline,
            keep_workspaces=args.keep_workspaces,
            live_model=args.live_model,
            live_profile=live_profile,
        )
    except RuntimeError as exc:
        # A refused preflight is a configuration problem the user can fix, not a
        # harness crash. A traceback here buries the one line that says what to do.
        raise SystemExit(str(exc)) from exc
    print(f"eval report: {output_dir / 'report.json'}")
    print(f"markdown: {output_dir / 'report.md'}")
    print(f"status: {'PASS' if report['passed'] else 'FAIL'}")
    return 0 if report["passed"] else 1


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
