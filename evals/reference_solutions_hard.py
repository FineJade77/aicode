"""Known-good solutions for the `live_hard` suite.

The `live` tier scores 84/84, so it cannot say where the Agent fails. These
tasks put the cause away from the symptom — the failing test names a module that
is not the one to edit. Each solution below is what "solvable" means for its
task; the test suite applies them and requires the suite to turn green, so an
unsolvable task is caught as an authoring bug rather than scored as a model
failure.
"""

from __future__ import annotations

HARD_SOLUTIONS: dict[str, dict[str, str]] = {
    # The key dropped kwargs, so every currency shared one cache entry.
    "hard_stale_cache": {
        "cache.py": '''_STORE: dict = {}


def memoize(fn):
    def wrapper(*args, **kwargs):
        key = (fn.__name__, args, tuple(sorted(kwargs.items())))
        if key not in _STORE:
            _STORE[key] = fn(*args, **kwargs)
        return _STORE[key]

    wrapper.cache_clear = _STORE.clear
    return wrapper
'''
    },
    # parse.py already normalises to UTC; the store applied the offset a second time.
    "hard_double_convert": {
        "store.py": '''class Store:
    def __init__(self):
        self.events = []

    def add(self, name, timestamp):
        self.events.append({"name": name, "at": timestamp})

    def all(self):
        return list(self.events)
'''
    },
    "hard_topo_order": {
        "graph.py": '''class CycleError(Exception):
    pass


def resolve(deps: dict[str, list[str]]) -> list[str]:
    ordered: list[str] = []
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        mark = state.get(node, 0)
        if mark == 1:
            raise CycleError(f"cycle through {node}")
        if mark == 2:
            return
        state[node] = 1
        for dependency in deps.get(node, []):
            visit(dependency)
        state[node] = 2
        ordered.append(node)

    for node in sorted(deps):
        visit(node)
    return ordered
'''
    },
    # Needs both files: half-up rounding in money, and stopping the per-line round in cart.
    "hard_money_cents": {
        "money.py": '''from decimal import ROUND_HALF_UP, Decimal


def round_cents(amount):
    return float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def format_amount(amount):
    return f"{round_cents(amount):.2f}"
''',
        "cart.py": '''class Cart:
    def __init__(self):
        self.lines = []

    def add_line(self, price, quantity):
        self.lines.append((price, quantity))

    def subtotal(self):
        total = 0
        for price, quantity in self.lines:
            total += price * quantity
        return total
''',
    },
    # The pager was right; the walk treated an empty page as the end of the data.
    "hard_filter_paging": {
        "collect.py": '''from page import fetch_page


def collect_all(rows, size=2):
    gathered = []
    cursor = 0
    while cursor is not None:
        page, cursor = fetch_page(rows, cursor, size)
        gathered.extend(page)
    return gathered
'''
    },
    "hard_hook_override": {
        "strict.py": '''from base import Pipeline


class StrictPipeline(Pipeline):
    def _validate(self, item):
        return bool(item and item.strip())

    def _apply(self, item):
        super()._apply(item)
        return item.strip().upper()
'''
    },
    "hard_spec_export": {
        "export.py": '''COLUMNS = ("id", "name", "notes")

NEEDS_QUOTING = (",", '"', "\\n")


def _field(value):
    text = "" if value is None else str(value)
    if any(character in text for character in NEEDS_QUOTING):
        escaped = text.replace('"', '""')
        return f'"{escaped}"'
    return text


def to_csv(rows):
    lines = ["id,name,notes"]
    for row in rows:
        lines.append(",".join(_field(row.get(column)) for column in COLUMNS))
    return "\\n".join(lines) + "\\n"
'''
    },
    "hard_deep_merge": {
        "merge.py": '''def merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = merge(existing, value)
        else:
            result[key] = value
    return result
'''
    },
}


# --- live_scale --------------------------------------------------------------
#
# `live_hard` showed indirection is free for a model that reads the whole repo.
# These test the untested axis — scale — where the symptom greps to dozens of
# equally plausible hits and reading everything stops being cheap.

SCALE_SOLUTIONS: dict[str, dict[str, str]] = {
    # One of 35 uniform handlers summed an amount where the contract says count.
    "scale_handler_contract": {
        "handlers/tariffs.py": '''"""Tariffs domain handler."""

from contracts import Result


def handle(records):
    """Summarise tariffs records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "tariffs":
            continue
        total += 1
    return Result(name="tariffs", total=total)
'''
    },
    # Two of 30 uniform sections misspelled a required key.
    "scale_config_drift": {
        "sections/warehouses.py": '''"""Warehouses settings."""

SECTION = "warehouses"

DEFAULTS = {
    "enabled": True,
    "retry_limit": 3,
    "timeout_seconds": 30,
}


def defaults():
    return dict(DEFAULTS)
''',
        "sections/coupons.py": '''"""Coupons settings."""

SECTION = "coupons"

DEFAULTS = {
    "enabled": True,
    "retry_limit": 3,
    "timeout_seconds": 30,
}


def defaults():
    return dict(DEFAULTS)
''',
    },
}

HARD_SOLUTIONS.update(SCALE_SOLUTIONS)

# The size-curve tier: identical fix at four repository sizes, so any difference
# in measured effort is attributable to size and nothing else.
from evals.reference_solutions_curve import CURVE_SOLUTIONS  # noqa: E402

HARD_SOLUTIONS.update(CURVE_SOLUTIONS)
