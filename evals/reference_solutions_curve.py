"""Known-good solutions for the `live_scale_curve` tier.

One defect, four repository sizes. The fix is identical in every case — only
the number of modules it hides among changes, which is the whole point: any
difference in measured effort is attributable to size and nothing else.
"""

from __future__ import annotations

CURVE_SOLUTIONS: dict[str, dict[str, str]] = {
    "scale_curve_010": {
        "handlers/order_sync.py": '''"""Order Sync handler."""

from contracts import Result


def handle(records):
    """Summarise order_sync records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "order_sync":
            continue
        total += 1
    return Result(name="order_sync", total=total)
''',
    },
    "scale_curve_030": {
        "handlers/export_sync.py": '''"""Export Sync handler."""

from contracts import Result


def handle(records):
    """Summarise export_sync records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "export_sync":
            continue
        total += 1
    return Result(name="export_sync", total=total)
''',
    },
    "scale_curve_100": {
        "handlers/export_sync.py": '''"""Export Sync handler."""

from contracts import Result


def handle(records):
    """Summarise export_sync records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "export_sync":
            continue
        total += 1
    return Result(name="export_sync", total=total)
''',
    },
    "scale_curve_300": {
        "handlers/export_batch.py": '''"""Export Batch handler."""

from contracts import Result


def handle(records):
    """Summarise export_batch records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "export_batch":
            continue
        total += 1
    return Result(name="export_batch", total=total)
''',
    },
}
