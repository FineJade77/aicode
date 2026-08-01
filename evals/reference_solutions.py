"""Known-good solutions for every live eval task.

A live task that no correct edit can satisfy reports a model failure that is
really a task-authoring bug, and a mutation the reference tests do not catch
means the task can be passed by a test with no teeth. Both are checked against
these solutions in the test suite.

They live here rather than in the fixtures on purpose: anything inside a fixture
is copied into the Agent's workspace, where it would be an answer key.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile

from evals.contracts import EvalTask

EVAL_ROOT = pathlib.Path(__file__).resolve().parent

SOLUTIONS: dict[str, dict[str, str]] = {
    "live_pagination": {
        "pagination.py": '''def paginate(items: list, page: int, per_page: int) -> list:
    start = (page - 1) * per_page
    return items[start : start + per_page]
'''
    },
    "live_amounts": {
        "amounts.py": '''def parse_amount(raw: str) -> int:
    return int(raw.strip().replace(",", ""))
'''
    },
    "live_basket": {
        "basket.py": '''def add_item(name: str, items: list | None = None) -> list:
    items = [] if items is None else items
    items.append(name)
    return items
'''
    },
    "live_roster": {
        "roster.py": '''def sort_names(names: list[str]) -> list[str]:
    return sorted(names, key=str.lower)
'''
    },
    "live_deadlines": {
        "deadlines.py": '''from datetime import datetime, timezone


def is_overdue(deadline: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline < now
'''
    },
    "live_retry": {
        "retry.py": '''from collections.abc import Callable
from typing import Any


def call_with_retries(operation: Callable[[], Any], attempts: int) -> Any:
    last: BaseException | None = None
    for _ in range(attempts):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last if last is not None else RuntimeError("no attempt was made")
'''
    },
    "live_percentiles": {
        "percentiles.py": '''import math


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    index = math.ceil(percent / 100 * len(ordered)) - 1
    return ordered[min(max(index, 0), len(ordered) - 1)]
'''
    },
    "live_money": {
        "money.py": '''def split_bill(total_cents: int, people: int) -> list[int]:
    share, remainder = divmod(total_cents, people)
    return [share + (1 if index < remainder else 0) for index in range(people)]
'''
    },
    "live_user_field": {
        "models.py": '''from dataclasses import dataclass


@dataclass
class User:
    id: int
    name: str
    email: str = ""
''',
        "serializer.py": '''from models import User


def serialize(user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email}
''',
    },
    "live_rename_api": {
        "store.py": '''RECORDS = [
    {"id": 1, "label": "alpha"},
    {"id": 2, "label": "beta"},
]


def list_records() -> list[dict]:
    return list(RECORDS)
''',
        "report.py": '''from store import list_records


def build_report() -> str:
    return ", ".join(record["label"] for record in list_records())
''',
    },
    "live_error_type": {
        "errors.py": '''class AppError(Exception):
    pass


class ConfigError(AppError):
    pass
''',
        "loader.py": '''from errors import ConfigError


def load(raw: dict) -> dict:
    if "name" not in raw:
        raise ConfigError("name")
    return raw
''',
        "validator.py": '''from errors import ConfigError


def validate(config: dict) -> dict:
    if config.get("retries", 0) < 0:
        raise ConfigError("retries must not be negative")
    return config
''',
    },
    "live_config_flag": {
        "config.py": '''from dataclasses import dataclass

KNOWN_KEYS = {"host", "port"}


@dataclass
class Config:
    host: str = "localhost"
    port: int = 8080
    strict: bool = False
''',
        "service.py": '''from config import KNOWN_KEYS, Config


def configure(config: Config, overrides: dict) -> dict:
    applied = {"host": config.host, "port": config.port}
    for key, value in overrides.items():
        if key in KNOWN_KEYS:
            applied[key] = value
        elif config.strict:
            raise ValueError(f"unknown key: {key}")
    return applied
''',
    },
    "live_move_helper": {
        "text_utils.py": '''def truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def slugify(title: str) -> str:
    return "-".join(part.lower() for part in title.split() if part)
''',
        "posts.py": '''from text_utils import slugify


def permalink(title: str) -> str:
    return f"/posts/{slugify(title)}"
''',
    },
    "live_protocol_method": {
        "storage.py": '''from typing import Protocol


class Storage(Protocol):
    def put(self, key: str, value: str) -> None: ...

    def get(self, key: str) -> str | None: ...

    def delete(self, key: str) -> None: ...
''',
        "memory_storage.py": '''class MemoryStorage:
    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def put(self, key: str, value: str) -> None:
        self.items[key] = value

    def get(self, key: str) -> str | None:
        return self.items.get(key)

    def delete(self, key: str) -> None:
        self.items.pop(key, None)
''',
        "file_storage.py": '''from pathlib import Path


class FileStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.txt"

    def put(self, key: str, value: str) -> None:
        self._path(key).write_text(value, encoding="utf-8")

    def get(self, key: str) -> str | None:
        path = self._path(key)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
''',
    },
    "live_normalize": {
        "normalize.py": '''def normalize(value: str) -> str:
    return value.strip().lower()
'''
    },
    "live_hidden_caller": {
        "row_format.py": '''SEPARATOR = ","


def format_row(cells: list[str]) -> str:
    return SEPARATOR.join(cells)
''',
        "report.py": '''from row_format import SEPARATOR, format_row


def build_report(rows: list[list[str]]) -> str:
    return "\\n".join(format_row(row) for row in rows)


def column_count(rendered_row: str) -> int:
    return len(rendered_row.split(SEPARATOR))
''',
    },
    "live_missing_constant": {
        "codes.py": '''HTTP_OK = 200
HTTP_CREATED = 201
'''
    },
    "live_median": {
        "median.py": '''def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
'''
    },
    # Category C: the solution is the test file the Agent is asked to write.
    "live_stats": {
        "test_stats.py": '''from stats import mean, spread


def test_mean_of_several_values():
    assert mean([1, 2, 3]) == 2


def test_spread_of_several_values():
    assert spread([1, 5]) == 4


def test_mean_of_nothing_is_zero():
    assert mean([]) == 0.0


def test_spread_of_nothing_is_zero():
    assert spread([]) == 0.0
'''
    },
    "live_unicode_slug": {
        "test_slug.py": '''from slug import slugify


def test_ascii_title():
    assert slugify("Hello There World") == "hello-there-world"


def test_keeps_non_ascii_letters():
    assert slugify("Café Crème") == "café-crème"


def test_keeps_han_characters():
    assert slugify("中文 标题") == "中文-标题"
'''
    },
    "live_parser": {
        "test_parser.py": '''import pytest

from parser import parse_pair


def test_parses_a_pair():
    assert parse_pair("host = example.com") == ("host", "example.com")


def test_missing_separator_raises():
    with pytest.raises(ValueError):
        parse_pair("host")


def test_empty_key_raises():
    with pytest.raises(ValueError):
        parse_pair("  = value")
'''
    },
    "live_clamp": {
        "test_clamp.py": '''from clamp import clamp


def test_value_inside_the_range_is_unchanged():
    assert clamp(5, 0, 10) == 5


def test_below_the_range():
    assert clamp(-1, 0, 10) == 0


def test_above_the_range():
    assert clamp(11, 0, 10) == 10


def test_on_the_lower_boundary():
    assert clamp(0, 0, 10) == 0


def test_on_the_upper_boundary():
    assert clamp(10, 0, 10) == 10
'''
    },
    "live_tags": {
        "test_tags.py": '''from tags import add_tag


def test_adds_a_new_tag():
    assert add_tag(["a"], "b") == ["a", "b"]


def test_adding_the_same_tag_twice_changes_nothing():
    assert add_tag(["a", "b"], "b") == ["a", "b"]


def test_repeated_application_is_stable():
    once = add_tag(["a"], "b")
    assert add_tag(once, "b") == once
'''
    },
    "live_csv_row": {
        "test_csv_row.py": '''from csv_row import split_row


def test_splits_a_plain_row():
    assert split_row("a,b,c") == ["a", "b", "c"]


def test_trailing_separator_keeps_the_empty_field():
    assert split_row("a,b,") == ["a", "b", ""]


def test_only_a_separator():
    assert split_row(",") == ["", ""]
'''
    },
}




def verify(task: EvalTask) -> list[str]:
    """Apply the reference solution and report what is still wrong.

    An empty list means the task is solvable and every mutation it declares is
    caught by the reference tests.
    """
    problems: list[str] = []
    solution = SOLUTIONS.get(task.fixture)
    if solution is None:
        return [f"no reference solution for fixture {task.fixture!r}"]
    fixture = EVAL_ROOT / "fixtures" / task.fixture
    command = task.checks.test_commands[0]
    with tempfile.TemporaryDirectory(prefix="aicode-reference-") as temp:
        work = pathlib.Path(temp) / "workspace"
        shutil.copytree(fixture, work, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for relative, content in solution.items():
            (work / relative).write_text(content, encoding="utf-8")
        if _run(command, work) != 0:
            problems.append("the reference solution does not make the suite pass")
        for index, mutation in enumerate(task.checks.mutations):
            mutant = pathlib.Path(temp) / f"mutant-{index}"
            shutil.copytree(work, mutant, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = mutant / mutation.path
            source = target.read_text(encoding="utf-8")
            if mutation.old_text not in source:
                problems.append(f"mutation {index + 1}: anchor missing from {mutation.path}")
                continue
            target.write_text(source.replace(mutation.old_text, mutation.new_text, 1), encoding="utf-8")
            if _run(task.checks.test_commands[mutation.command_index], mutant) == 0:
                problems.append(f"mutation {index + 1}: reference tests do not catch it")
    return problems


def _run(command: list[str], cwd: pathlib.Path) -> int:
    # Plugin autoload dominates the runtime here (roughly 1.9s vs 0.5s per
    # invocation across ~34 of them), and every fixture suite uses core pytest
    # only, so disabling it changes the wall time and nothing else. The grader
    # itself deliberately does not do this: what it runs has to match what the
    # Agent ran.
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    ).returncode
