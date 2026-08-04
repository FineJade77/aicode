"""Known-good solutions for the `live_coordinated` suite.

Every task here is only correct when the change lands in several files at once,
and each file needs a *different* edit — find-and-replace is deliberately
insufficient. That makes "solvable" a stronger claim than in the other tiers, so
these solutions exist to prove the whole coordinated change is expressible, not
just the first file of it.
"""

from __future__ import annotations

COORDINATED_SOLUTIONS: dict[str, dict[str, str]] = {
    # Milliseconds at the source; the report reformats, the threshold rescales,
    # and the aggregate divides by a different constant. Three different edits.
    "coord_units": {
        "timing.py": '''"""Duration measurement."""


def elapsed(start, end):
    """Return the elapsed time in milliseconds."""
    return (end - start) * 1000
''',
        "report.py": '''"""Human-readable run reports."""

from timing import elapsed


def format_duration(start, end):
    return f"{elapsed(start, end):.0f}ms"
''',
        "alerts.py": '''"""Slow-run alerting."""

from timing import elapsed

SLOW_THRESHOLD = 2500


def is_slow(start, end):
    return elapsed(start, end) > SLOW_THRESHOLD
''',
        "budget.py": '''"""Aggregate time spent."""

from timing import elapsed


def total_minutes(spans):
    return sum(elapsed(start, end) for start, end in spans) / 60000
''',
    },
    # The variant has to be declared once and answered for three times, with a
    # different answer each time.
    "coord_variant": {
        "kinds.py": '''"""Supported record kinds."""

KINDS = ("order", "refund", "chargeback")
''',
        "pricing.py": '''"""Per-kind pricing."""

from kinds import KINDS

SIGNS = {"order": 1, "refund": -1, "chargeback": -1}


def sign_for(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return SIGNS[kind]
''',
        "labels.py": '''"""Per-kind display labels."""

from kinds import KINDS

LABELS = {"order": "Order", "refund": "Refund", "chargeback": "Chargeback"}


def label_for(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return LABELS[kind]
''',
        "routing.py": '''"""Per-kind queue routing."""

from kinds import KINDS

QUEUES = {"order": "billing", "refund": "billing-reversals", "chargeback": "disputes"}


def queue_for(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return QUEUES[kind]
''',
    },
    # The argument becomes required in one place and is supplied with a
    # different value in each of the three others.
    "coord_argument": {
        "audit.py": '''"""Audit record construction."""


def record(action, detail, severity):
    """Build one audit entry."""
    return {"action": action, "detail": detail, "severity": severity}
''',
        "login.py": '''"""Authentication events."""

from audit import record


def failed_login(user):
    return record("login.failed", user, "warning")
''',
        "billing.py": '''"""Billing events."""

from audit import record


def charge_declined(invoice):
    return record("billing.declined", invoice, "error")
''',
        "admin.py": '''"""Administrative events."""

from audit import record


def role_granted(target):
    return record("admin.role_granted", target, "critical")
''',
    },
    # The shared constant becomes a per-caller argument, and each caller picks a
    # different ceiling.
    "coord_rule": {
        "limits.py": '''"""Per-field submission limits."""


def check_length(text, max_length):
    if len(text) > max_length:
        raise ValueError("too long")
    return text
''',
        "titles.py": '''"""Title submission."""

from limits import check_length

MAX_TITLE = 80


def submit_title(text):
    return check_length(text, MAX_TITLE)
''',
        "comments.py": '''"""Comment submission."""

from limits import check_length

MAX_COMMENT = 1000


def submit_comment(text):
    return check_length(text, MAX_COMMENT)
''',
        "bios.py": '''"""Profile bio submission."""

from limits import check_length

MAX_BIO = 300


def submit_bio(text):
    return check_length(text, MAX_BIO)
''',
    },
}
