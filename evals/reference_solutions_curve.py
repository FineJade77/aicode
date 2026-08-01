"""Known-good solutions for the `live_scale_curve` tier.

One defect, four repository sizes. The fix is identical in every case — only
the number of modules it hides among changes, which is the whole point: any
difference in measured effort is attributable to size and nothing else.
"""

from __future__ import annotations

CURVE_SOLUTIONS: dict[str, dict[str, str]] = {
    "scale_curve_010": {
        "handlers/refund_sync.py": '''"""Refund Sync handler."""

from contracts import Result


def handle(records):
    """Summarise refund_sync records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "refund_sync":
            continue
        total += 1
    return Result(name="refund_sync", total=total)
''',
    },
    "scale_curve_030": {
        "handlers/carrier_sync.py": '''"""Carrier Sync handler."""

from contracts import Result


def handle(records):
    """Summarise carrier_sync records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "carrier_sync":
            continue
        total += 1
    return Result(name="carrier_sync", total=total)
''',
    },
    "scale_curve_100": {
        "handlers/refund_cache.py": '''"""Refund Cache handler."""

from contracts import Result


def handle(records):
    """Summarise refund_cache records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "refund_cache":
            continue
        total += 1
    return Result(name="refund_cache", total=total)
''',
    },
    "scale_curve_300": {
        "handlers/carrier_queue.py": '''"""Carrier Queue handler."""

from contracts import Result


def handle(records):
    """Summarise carrier_queue records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "carrier_queue":
            continue
        total += 1
    return Result(name="carrier_queue", total=total)
''',
    },
}
