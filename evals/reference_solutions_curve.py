"""Known-good solutions for the `live_scale_curve` tier.

One defect, four repository sizes. The fix is identical in every case — only
the number of modules it hides among changes, which is the whole point: any
difference in measured effort is attributable to size and nothing else.
"""

from __future__ import annotations

CURVE_SOLUTIONS: dict[str, dict[str, str]] = {
    "scale_curve_010": {
        "handlers/product_sync.py": '''"""Product Sync handler."""

from contracts import Result


def handle(records):
    """Summarise product_sync records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "product_sync":
            continue
        total += 1
    return Result(name="product_sync", total=total)
''',
    },
    "scale_curve_030": {
        "handlers/review_sync.py": '''"""Review Sync handler."""

from contracts import Result


def handle(records):
    """Summarise review_sync records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "review_sync":
            continue
        total += 1
    return Result(name="review_sync", total=total)
''',
    },
    "scale_curve_100": {
        "handlers/role_cache.py": '''"""Role Cache handler."""

from contracts import Result


def handle(records):
    """Summarise role_cache records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "role_cache":
            continue
        total += 1
    return Result(name="role_cache", total=total)
''',
    },
    "scale_curve_300": {
        "handlers/order_log.py": '''"""Order Log handler."""

from contracts import Result


def handle(records):
    """Summarise order_log records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "order_log":
            continue
        total += 1
    return Result(name="order_log", total=total)
''',
    },
}
