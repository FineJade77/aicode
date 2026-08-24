"""Structured compaction summaries.

Free-text summaries degrade fast under chained compaction — summarizing a
summary loses whichever facts the model happened not to repeat, and nothing can
check what went missing because there is no shape to check against. A fixed set
of fields makes both the carry-forward and the loss programmatically decidable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

SUMMARY_FIELDS = ("goal", "constraints", "done", "pending", "files_touched", "open_failures")
LIST_FIELDS = ("constraints", "done", "pending", "files_touched", "open_failures")
MAX_ITEMS_PER_FIELD = 40
MAX_ITEM_CHARS = 400
MAX_GOAL_CHARS = 600


@dataclass(slots=True)
class StructuredSummary:
    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    open_failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "constraints": list(self.constraints),
            "done": list(self.done),
            "pending": list(self.pending),
            "files_touched": list(self.files_touched),
            "open_failures": list(self.open_failures),
        }

    def is_empty(self) -> bool:
        return not self.goal and not any(getattr(self, name) for name in LIST_FIELDS)

    def render(self) -> str:
        """Render to the text that goes into the projected history message.

        Deterministic, so the same structure always produces the same prompt
        bytes — a summary that reshuffled itself between turns would invalidate
        prompt caching for everything after it.
        """
        lines: list[str] = []
        if self.goal:
            lines.append(f"Goal: {self.goal}")
        for label, values in (
            ("Constraints", self.constraints),
            ("Done", self.done),
            ("Pending", self.pending),
            ("Files touched", self.files_touched),
            ("Open failures", self.open_failures),
        ):
            if not values:
                continue
            lines.append(f"{label}:")
            lines.extend(f"- {value}" for value in values)
        return "\n".join(lines)


def _clean_item(value: Any) -> str:
    return " ".join(str(value).split())[:MAX_ITEM_CHARS]


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        # A model that returns a string where a list belongs is close enough to
        # correct to keep; rejecting it would throw away a usable summary.
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in items:
        if isinstance(raw, dict | list):
            continue
        item = _clean_item(raw)
        if not item or item in seen:
            continue
        seen.add(item)
        cleaned.append(item)
        if len(cleaned) >= MAX_ITEMS_PER_FIELD:
            break
    return cleaned


def parse_structured_summary(text: str) -> StructuredSummary | None:
    """Parse the summarizer's reply, or return None if it is not usable.

    None means "fall back to the deterministic summary". A partially-correct
    object is accepted and normalised: the alternative — discarding a summary
    because one field came back as a string instead of a list — loses far more
    than it protects.
    """
    payload = _load_json_object(text)
    if payload is None:
        return None
    if not any(key in payload for key in SUMMARY_FIELDS):
        return None
    summary = StructuredSummary(
        goal=_clean_item(payload.get("goal") or "")[:MAX_GOAL_CHARS],
        constraints=_clean_list(payload.get("constraints")),
        done=_clean_list(payload.get("done")),
        pending=_clean_list(payload.get("pending")),
        files_touched=_clean_list(payload.get("files_touched")),
        open_failures=_clean_list(payload.get("open_failures")),
    )
    return None if summary.is_empty() else summary


def _load_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        # Models fence JSON even when told not to; unwrapping is cheaper than
        # discarding an otherwise valid summary.
        newline = raw.find("\n")
        if newline == -1:
            return None
        raw = raw[newline + 1 :]
        fence = raw.rfind("```")
        if fence != -1:
            raw = raw[:fence]
        raw = raw.strip()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(raw[start : end + 1])
        except (ValueError, TypeError):
            return None
    return payload if isinstance(payload, dict) else None


def merge_summaries(previous: StructuredSummary | None, current: StructuredSummary) -> StructuredSummary:
    """Carry unfinished work forward instead of trusting the model to repeat it.

    This is the point of the structure. Under chained compaction the model is
    summarizing a summary, and anything it omits is gone for good. `pending` and
    `open_failures` are the two fields where silent loss actually costs work, so
    they are re-added programmatically.

    An item leaves `pending`/`open_failures` only by appearing in the new `done`
    list. Matching is by normalised text, which is imprecise — but it is
    imprecise in the safe direction: a near-miss keeps a finished item listed as
    pending, where the worst case is redundant work, rather than dropping an
    unfinished one, where the worst case is silently abandoning it.
    """
    if previous is None:
        return current
    resolved = {item.casefold() for item in current.done}
    merged = StructuredSummary(
        goal=current.goal or previous.goal,
        constraints=_union(previous.constraints, current.constraints),
        done=_union(previous.done, current.done),
        files_touched=_union(previous.files_touched, current.files_touched),
        pending=_carry_forward(previous.pending, current.pending, resolved),
        open_failures=_carry_forward(previous.open_failures, current.open_failures, resolved),
    )
    return merged


def _union(previous: list[str], current: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for item in [*current, *previous]:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= MAX_ITEMS_PER_FIELD:
            break
    return merged


def _carry_forward(previous: list[str], current: list[str], resolved: set[str]) -> list[str]:
    kept = [item for item in previous if item.casefold() not in resolved]
    return _union(kept, current)
