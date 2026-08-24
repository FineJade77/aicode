#!/usr/bin/env python3
"""Read the retrieval-cost curve out of a `live_scale_curve` report.

    python3 scripts/eval_curve.py .artifacts/evals/live_scale_curve-<id>

Every tier so far has scored a functional pass@1 of 1.000, so the verdict column
has stopped carrying information. What moves is retrieval cost. This prints
effort against repository size and the growth exponent, because the decision a
repo map (T-047) turns on is whether that curve bends upward or stays flat — and
a table of four numbers makes that visible where a pass/fail summary does not.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

METRICS = ("tool_calls", "model_calls", "input_tokens", "duration_ms")


def module_count(task_id: str) -> int | None:
    match = re.search(r"(\d+)$", task_id)
    return int(match.group(1)) if match else None


def growth_exponent(sizes: list[int], values: list[float]) -> float | None:
    """Least-squares slope of log(value) against log(size).

    ~0 means flat (size is free), ~1 linear, >1 superlinear. Reported rather
    than a verdict because two points of noise should not be dressed up as a
    conclusion — with four sizes this is an indication, not a fit.
    """
    points = [(math.log(s), math.log(v)) for s, v in zip(sizes, values, strict=True) if s > 0 and v > 0]
    if len(points) < 2:
        return None
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if not denominator:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator


def main(report_dir: str) -> int:
    report = json.loads((Path(report_dir) / "report.json").read_text(encoding="utf-8"))

    # A run the provider cut short spent a fraction of the effort the task
    # needed, so it does not lower the curve honestly — it lowers it toward
    # zero. Refusing is the point: an outage flattens the exponent in exactly
    # the direction that reads as "size is free", and a flat curve printed from
    # three aborted runs is indistinguishable from a real one.
    aborted = [
        run["task_id"]
        for run in report["runs"]
        if run["metrics"].get("failure_reason") == "provider_unavailable"
    ]
    if aborted:
        print(
            f"refusing to plot: {len(aborted)} run(s) ended on a provider outage "
            f"({', '.join(sorted(set(aborted)))}). Those runs measured the outage, "
            "not retrieval. Rerun the suite.",
            file=sys.stderr,
        )
        return 2

    rows: dict[int, dict] = {}
    for run in report["runs"]:
        size = module_count(run["task_id"])
        if size is None:
            continue
        # Average across repetitions so a rerun does not change the shape.
        bucket = rows.setdefault(size, {"n": 0, **{metric: 0.0 for metric in METRICS}})
        bucket["n"] += 1
        for metric in METRICS:
            bucket[metric] += run["metrics"].get(metric, 0)

    if not rows:
        print("no size-suffixed tasks in this report", file=sys.stderr)
        return 1

    sizes = sorted(rows)
    print(f"{'modules':>8}  {'tool calls':>10}  {'model calls':>11}  {'input tokens':>12}  {'seconds':>8}")
    for size in sizes:
        bucket = rows[size]
        n = bucket["n"]
        print(
            f"{size:>8}  {bucket['tool_calls']/n:>10.1f}  {bucket['model_calls']/n:>11.1f}  "
            f"{bucket['input_tokens']/n:>12,.0f}  {bucket['duration_ms']/n/1000:>8.1f}"
        )

    print("\ngrowth exponent (log effort / log modules):")
    for metric in ("tool_calls", "model_calls", "input_tokens"):
        exponent = growth_exponent(sizes, [rows[size][metric] / rows[size]["n"] for size in sizes])
        if exponent is None:
            continue
        shape = "flat" if exponent < 0.25 else "sublinear" if exponent < 0.8 else "linear+"
        print(f"  {metric:<14} {exponent:5.2f}  ({shape})")
    print(
        "\nFlat means repository size is close to free and a repo map buys little.\n"
        "Rising means retrieval is the cost, which is what T-047 exists to compress."
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: eval_curve.py <report-dir>")
    raise SystemExit(main(sys.argv[1]))
