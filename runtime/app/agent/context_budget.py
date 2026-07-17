from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ContextBudget:
    total_chars: int
    default_text_chars: int
    compact_text_chars: int
    minimum_text_chars: int
    data_chars: int
    tool_text_chars: dict[str, int] = field(default_factory=dict)


CONTEXT_BUDGETS: dict[str, ContextBudget] = {
    "planner": ContextBudget(
        total_chars=28_000,
        default_text_chars=3_000,
        compact_text_chars=700,
        minimum_text_chars=220,
        data_chars=4_000,
        tool_text_chars={
            "read_file": 6_000,
            "git_diff": 8_000,
            "review_diff": 6_000,
            "run_tests": 5_000,
            "run_shell": 5_000,
        },
    ),
    "coder": ContextBudget(
        total_chars=52_000,
        default_text_chars=5_000,
        compact_text_chars=900,
        minimum_text_chars=280,
        data_chars=6_000,
        tool_text_chars={
            "read_file": 18_000,
            "git_diff": 18_000,
            "review_diff": 10_000,
            "run_tests": 12_000,
            "run_shell": 12_000,
            "apply_patch": 12_000,
            "search_text": 4_000,
            "find_files": 4_000,
        },
    ),
    "reviewer": ContextBudget(
        total_chars=52_000,
        default_text_chars=6_000,
        compact_text_chars=900,
        minimum_text_chars=280,
        data_chars=6_000,
        tool_text_chars={
            "git_diff": 22_000,
            "review_diff": 18_000,
            "read_file": 12_000,
            "run_tests": 12_000,
            "run_shell": 12_000,
        },
    ),
    "summarizer": ContextBudget(
        total_chars=32_000,
        default_text_chars=4_000,
        compact_text_chars=700,
        minimum_text_chars=220,
        data_chars=4_000,
        tool_text_chars={
            "read_file": 8_000,
            "git_diff": 8_000,
            "run_tests": 8_000,
            "run_shell": 8_000,
            "apply_patch": 10_000,
        },
    ),
}

LOW_PRIORITY_TOOLS = {"list_files", "detect_project", "git_status", "find_files", "search_text"}
HIGH_PRIORITY_TOOLS = {"read_file", "git_diff", "review_diff", "run_tests", "run_shell", "apply_patch"}


def budgeted_observations_for_model(observations: list[dict[str, Any]], purpose: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    budget = CONTEXT_BUDGETS.get(purpose, CONTEXT_BUDGETS["summarizer"])
    compacted = [compact_observation(observation, budget) for observation in observations]
    per_observation_compactions = sum(1 for observation in compacted if "context_compacted" in observation)

    before_chars = encoded_chars(compacted)
    total_budget_compactions = enforce_total_budget(compacted, budget)
    after_chars = encoded_chars(compacted)

    stats = {
        "purpose": purpose,
        "total_budget_chars": budget.total_chars,
        "estimated_observation_chars": after_chars,
        "input_observations": len(observations),
        "output_observations": len(compacted),
        "compacted": per_observation_compactions > 0 or total_budget_compactions > 0 or before_chars > budget.total_chars,
        "per_observation_compactions": per_observation_compactions,
        "total_budget_compactions": total_budget_compactions,
        "compacted_observations": compacted_observation_summaries(compacted),
    }
    return compacted, stats


def compact_observation(observation: dict[str, Any], budget: ContextBudget) -> dict[str, Any]:
    item = copy.deepcopy(observation)
    tool = str(item.get("tool") or "")

    if "text" in item:
        text = str(item.get("text") or "")
        compacted_text, metadata = compact_text(text, text_limit_for_tool(tool, budget))
        item["text"] = compacted_text
        if metadata:
            mark_context_compacted(item, "text", metadata)

    if isinstance(item.get("data"), dict):
        compacted_data, metadata = compact_data(item["data"], budget.data_chars)
        item["data"] = compacted_data
        if metadata:
            mark_context_compacted(item, "data", metadata)

    return item


def enforce_total_budget(observations: list[dict[str, Any]], budget: ContextBudget) -> int:
    compactions = 0
    if encoded_chars(observations) <= budget.total_chars:
        return compactions

    for index in sorted(range(len(observations)), key=lambda item: (compression_priority(observations[item]), item)):
        if encoded_chars(observations) <= budget.total_chars:
            break
        text = str(observations[index].get("text") or "")
        if len(text) <= budget.compact_text_chars:
            continue
        observations[index]["text"], metadata = compact_text(text, budget.compact_text_chars)
        metadata["reason"] = "total_context_budget"
        mark_context_compacted(observations[index], "text", metadata)
        compactions += 1

    for index in sorted(range(len(observations)), key=lambda item: (compression_priority(observations[item]), item)):
        if encoded_chars(observations) <= budget.total_chars:
            break
        text = str(observations[index].get("text") or "")
        if len(text) <= budget.minimum_text_chars:
            continue
        observations[index]["text"], metadata = compact_text(text, budget.minimum_text_chars)
        metadata["reason"] = "minimum_context_budget"
        mark_context_compacted(observations[index], "text", metadata)
        compactions += 1

    for index in sorted(range(len(observations)), key=lambda item: (compression_priority(observations[item]), item)):
        if encoded_chars(observations) <= budget.total_chars:
            break
        text = str(observations[index].get("text") or "")
        if not text or text.startswith("[CONTEXT OMITTED:"):
            continue
        observations[index]["text"] = f"[CONTEXT OMITTED: {len(text)} chars omitted due to prompt budget; metadata kept.]"
        mark_context_compacted(
            observations[index],
            "text",
            {
                "original_chars": len(text),
                "kept_chars": len(observations[index]["text"]),
                "reason": "metadata_only",
            },
        )
        compactions += 1

    return compactions


def compact_text(text: str, limit: int) -> tuple[str, dict[str, Any]]:
    if len(text) <= limit:
        return text, {}
    marker = f"\n...[CONTEXT COMPACTED: {len(text) - limit} chars omitted]...\n"
    if limit <= len(marker) + 20:
        compacted = text[: max(0, limit - len(marker))] + marker
    else:
        remaining = limit - len(marker)
        head = max(1, int(remaining * 0.65))
        tail = max(0, remaining - head)
        compacted = text[:head] + marker + (text[-tail:] if tail else "")
    return compacted, {"original_chars": len(text), "kept_chars": len(compacted)}


def compact_data(data: dict[str, Any], limit: int) -> tuple[dict[str, Any], dict[str, Any]]:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) <= limit:
        return data, {}
    preview = encoded[:limit]
    return (
        {
            "context_compacted": True,
            "original_chars": len(encoded),
            "preview": preview,
        },
        {"original_chars": len(encoded), "kept_chars": len(preview), "reason": "data_budget"},
    )


def mark_context_compacted(observation: dict[str, Any], key: str, metadata: dict[str, Any]) -> None:
    existing = observation.get("context_compacted")
    if not isinstance(existing, dict):
        existing = {}
    existing[key] = metadata
    observation["context_compacted"] = existing


def text_limit_for_tool(tool: str, budget: ContextBudget) -> int:
    return budget.tool_text_chars.get(tool, budget.default_text_chars)


def compression_priority(observation: dict[str, Any]) -> int:
    tool = str(observation.get("tool") or "")
    if tool in LOW_PRIORITY_TOOLS:
        return 0
    if tool in HIGH_PRIORITY_TOOLS:
        return 2
    return 1


def compacted_observation_summaries(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for observation in observations:
        compacted = observation.get("context_compacted")
        if not isinstance(compacted, dict):
            continue
        data = observation.get("data") if isinstance(observation.get("data"), dict) else {}
        args = observation.get("args") if isinstance(observation.get("args"), dict) else {}
        text_meta = compacted.get("text") if isinstance(compacted.get("text"), dict) else {}
        data_meta = compacted.get("data") if isinstance(compacted.get("data"), dict) else {}
        summaries.append(
            {
                "tool": str(observation.get("tool") or ""),
                "path": str(data.get("path") or args.get("path") or ""),
                "query": str(data.get("query") or args.get("query") or ""),
                "workspace": str(data.get("workspace") or args.get("workspace") or "main"),
                "text_original_chars": text_meta.get("original_chars"),
                "text_kept_chars": text_meta.get("kept_chars"),
                "data_original_chars": data_meta.get("original_chars"),
                "data_kept_chars": data_meta.get("kept_chars"),
            }
        )
    return summaries


def encoded_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
