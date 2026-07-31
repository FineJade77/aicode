from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.session import COMPACTION_SCHEMA_VERSION, AgentSession, CompactionEntry
from app.agent.summary import StructuredSummary, merge_summaries, parse_structured_summary
from app.agent.turn import MESSAGE_META_KEY
from app.models.provider import ModelCapability, ProviderError
from app.security import redact_known_environment_secrets

HISTORY_TOKEN_BUDGET = 60_000
HARD_BUDGET_FACTOR = 1.5
KEEP_RECENT_MESSAGES = 8
KEEP_RECENT_GROUPS = 6
# How many trailing message groups keep their tool output verbatim when folding.
# Tried from this value downwards, so the fold is only ever as aggressive as it
# needs to be.
FOLD_KEEP_RECENT_GROUPS = 4
FORCED_KEEP_RECENT_GROUPS = 1
COMPACTION_PROMPT_VERSION = "aicode.compaction.v2"
COMPACTION_SUMMARY_MAX_CHARS = 8_000

TOOL_OUTPUT_LIMITS = {"bash": 8_000, "run_tests": 8_000, "read_file": 0, "default": 6_000}

COMPACTION_SYSTEM_PROMPT = """\
You compress coding-agent history into a factual summary that is sufficient to continue the task. Do not speculate.
Preserve the user's goal and latest constraints, unfinished work, key decisions, recent file changes, validation results
and failures, approval or rejection outcomes, important paths, commands, errors, and tool results needed to continue.
Do not copy long tool output. Keep conclusions and clues for retrieving details again.

Reply with a single JSON object and nothing else, using exactly these keys:
{"goal": "<one sentence>", "constraints": [], "done": [], "pending": [], "files_touched": [], "open_failures": []}
- goal: the user's current objective.
- constraints: instructions and limits that still apply.
- done: work already completed. Move an item here verbatim from pending once it is finished.
- pending: work that still remains. Never drop an item unless it is now listed in done.
- files_touched: workspace-relative paths that were read or modified.
- open_failures: failing tests, commands, or errors that are not yet resolved.
Every list holds short plain strings. Use an empty list rather than omitting a key."""


@dataclass(frozen=True, slots=True)
class MessageGroup:
    start: int
    end: int
    complete: bool


class ContextManager:
    """Model-aware, persistent context preparation owned by Agent Core."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def prepare(
        self,
        *,
        session: AgentSession,
        purpose: str,
        model: str | None = None,
        system: str,
        tools: list[dict[str, Any]] | tuple[Any, ...],
        max_tokens: int,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        return await prepare_history_for_model(
            runtime=self.runtime,
            session=session,
            purpose=purpose,
            model=model,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            force=force,
        )


def load_history(session: AgentSession) -> list[dict[str, Any]]:
    return strip_message_meta(_history_with_meta(session))


def strip_message_meta(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop Runtime-private message metadata before a provider sees it.

    Providers reject unknown message keys, so a leak here is an outage rather
    than a cosmetic problem. Applied at every point history is handed outward.
    """
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        if MESSAGE_META_KEY in message:
            message = {key: value for key, value in message.items() if key != MESSAGE_META_KEY}
        cleaned.append(message)
    return cleaned


def _history_with_meta(session: AgentSession) -> list[dict[str, Any]]:
    messages, ids = _normalized_messages(session)
    compaction = latest_valid_compaction(session)
    if compaction is None:
        return messages
    projected = [_summary_message(compaction.summary)]
    projected.extend(
        message
        for message, message_id in zip(messages, ids, strict=False)
        if message_id > compaction.end_message_id
    )
    return projected


def persist_message(session: AgentSession, message: dict[str, Any]) -> int | None:
    return session.append_message(message)


def latest_valid_compaction(session: AgentSession) -> CompactionEntry | None:
    ids = {message_id for message_id in _normalized_message_ids(session) if message_id > 0}
    if not ids:
        return None
    for entry in reversed(session.compactions):
        if (
            entry.schema_version == COMPACTION_SCHEMA_VERSION
            and entry.session_id == session.session_id
            and entry.start_message_id in ids
            and entry.end_message_id in ids
            and entry.start_message_id <= entry.end_message_id
        ):
            return entry
    return None


def truncate_tool_output(tool_name: str, text: str) -> str:
    limit = TOOL_OUTPUT_LIMITS.get(tool_name, TOOL_OUTPUT_LIMITS["default"])
    if limit <= 0 or len(text) <= limit:
        return text
    marker = f"\n[output truncated: {len(text) - limit} characters omitted; use offset or pagination to continue]\n"
    head = int(limit * 0.65)
    tail = limit - head
    return text[:head] + marker + text[-tail:]


def estimate_tokens(messages: Any, chars_per_token: float = 3.5) -> int:
    serialized = json.dumps(messages, ensure_ascii=False, default=str, separators=(",", ":"))
    return max(1, int(len(serialized) / max(1.0, chars_per_token)))


def estimate_prompt_tokens(
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | tuple[Any, ...],
    chars_per_token: float = 3.5,
) -> int:
    return estimate_tokens({"system": system, "messages": messages, "tools": list(tools)}, chars_per_token)


async def prepare_history_for_model(
    *,
    runtime: Any,
    session: AgentSession,
    purpose: str,
    model: str | None = None,
    system: str,
    tools: list[dict[str, Any]] | tuple[Any, ...],
    max_tokens: int,
    force: bool = False,
) -> list[dict[str, Any]]:
    history = _history_with_meta(session)
    capability = _capability(runtime, purpose, max_tokens, model=model)
    context_settings = getattr(getattr(runtime.model_runtime, "settings", None), "context", None)
    chars_per_token = float(getattr(capability, "chars_per_token", getattr(context_settings, "chars_per_token", 3.5)))
    reserve_tokens = int(getattr(context_settings, "reserve_tokens", 1_024))
    threshold = float(getattr(context_settings, "compact_threshold", 0.8))
    requested_output = min(max_tokens, capability.max_output_tokens)
    hard_input_limit = max(1, capability.context_window - requested_output - reserve_tokens)
    proactive_limit = max(1, int(capability.context_window * threshold))
    input_limit = min(hard_input_limit, proactive_limit)
    before = estimate_prompt_tokens(system, history, tools, chars_per_token)
    if not force and before <= input_limit:
        return strip_message_meta(history)

    # Middle tier. Between per-tool truncation at write time and a lossy summary
    # of the whole history there was nothing, so exceeding the threshold by a
    # little spent a model call and discarded detail that folding alone could
    # have recovered. Folding drops only bulk tool output; user constraints and
    # assistant decisions stay verbatim, so nothing that drives the next step is
    # lost.
    history, folded_count = _fold_to_fit(
        history,
        system=system,
        tools=tools,
        chars_per_token=chars_per_token,
        input_limit=input_limit,
        stop_when_fits=not force,
    )
    if folded_count and not force:
        after = estimate_prompt_tokens(system, history, tools, chars_per_token)
        if after <= input_limit:
            await _emit_budget_event(
                session,
                capability,
                before_tokens=before,
                after_tokens=after,
                compacted=False,
                forced=False,
                reason="folded",
                folded_tool_outputs=folded_count,
            )
            return strip_message_meta(history)

    compacted = await _persist_compaction(
        runtime=runtime,
        session=session,
        current_history=history,
        capability=capability,
        before_tokens=before,
        context_window=capability.context_window,
        input_limit=input_limit,
        chars_per_token=chars_per_token,
        system=system,
        tools=tools,
        force=force,
    )
    return strip_message_meta(compacted)



FOLDED_TOOL_OUTPUT = "[earlier {tool} output folded to save context ({chars} characters); re-run it if the detail matters]"


def _fold_to_fit(
    messages: list[dict[str, Any]],
    *,
    system: str,
    tools: list[dict[str, Any]] | tuple[Any, ...],
    chars_per_token: float,
    input_limit: int,
    stop_when_fits: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Fold as little tool output as gets the history under the limit.

    Walks `keep` downwards so a history that only just overflows keeps almost
    everything verbatim. When nothing fits, the most-folded attempt is returned
    anyway: summarization is next, and it may as well start from less bulk.
    """
    best: list[dict[str, Any]] = messages
    best_count = 0
    for keep in range(FOLD_KEEP_RECENT_GROUPS, 0, -1):
        candidate, count = fold_old_tool_output(messages, keep_recent_groups=keep)
        if not count:
            continue
        best, best_count = candidate, count
        if stop_when_fits and estimate_prompt_tokens(system, candidate, tools, chars_per_token) <= input_limit:
            return candidate, count
    return best, best_count


def fold_old_tool_output(
    messages: list[dict[str, Any]],
    *,
    keep_recent_groups: int,
) -> tuple[list[dict[str, Any]], int]:
    """Replace older tool results with a one-line reference.

    Only `role == "tool"` messages are folded. That is what makes this tier
    lossless for the information that decides the next step: the user's goal and
    constraints and the assistant's own reasoning are never touched, only the
    bulk output they were derived from.

    Incomplete groups are left alone. An unresolved tool call and its result must
    stay together and intact, the same boundary rule compaction uses.
    """
    groups = _message_groups(messages)
    if len(groups) <= keep_recent_groups:
        return messages, 0
    foldable = groups[: len(groups) - keep_recent_groups] if keep_recent_groups else groups

    folded: dict[int, str] = {}
    for group in foldable:
        if not group.complete:
            continue
        names = _tool_call_names(messages[group.start])
        for index in range(group.start, group.end + 1):
            message = messages[index]
            if message.get("role") != "tool":
                continue
            content = str(message.get("content") or "")
            tool = names.get(str(message.get("tool_call_id") or "")) or "tool"
            replacement = FOLDED_TOOL_OUTPUT.format(tool=tool, chars=len(content))
            if len(replacement) >= len(content):
                # Folding a short result would cost more than it saves.
                continue
            folded[index] = replacement
    if not folded:
        return messages, 0

    updated: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index not in folded:
            updated.append(message)
            continue
        replacement = {key: value for key, value in message.items() if key != MESSAGE_META_KEY}
        replacement["content"] = folded[index]
        updated.append(replacement)
    return updated, len(folded)


def _tool_call_names(message: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("id"):
            names[str(call["id"])] = str(call.get("name") or "tool")
    return names


async def _persist_compaction(
    *,
    runtime: Any,
    session: AgentSession,
    current_history: list[dict[str, Any]],
    capability: ModelCapability,
    before_tokens: int,
    context_window: int,
    input_limit: int,
    chars_per_token: float,
    system: str,
    tools: list[dict[str, Any]] | tuple[Any, ...],
    force: bool,
) -> list[dict[str, Any]]:
    messages, ids = _normalized_messages(session)
    previous = latest_valid_compaction(session)
    previous_end = previous.end_message_id if previous is not None else 0
    new_records = [(message, message_id) for message, message_id in zip(messages, ids, strict=False) if message_id > previous_end]
    groups = _message_groups([message for message, _message_id in new_records])
    keep_counts = (
        [FORCED_KEEP_RECENT_GROUPS]
        if force
        else list(range(min(KEEP_RECENT_GROUPS, len(groups)), 0, -1))
    )
    cutoff_index: int | None = None
    for keep_count in keep_counts:
        candidate_groups = groups[:-keep_count] if len(groups) > keep_count else []
        eligible_groups: list[MessageGroup] = []
        for group in candidate_groups:
            if not group.complete:
                break
            eligible_groups.append(group)
        if not eligible_groups:
            continue
        candidate_cutoff = eligible_groups[-1].end
        candidate_end_id = new_records[candidate_cutoff][1]
        conservative_summary = _summary_message("x" * COMPACTION_SUMMARY_MAX_CHARS)
        candidate_projection = [conservative_summary]
        candidate_projection.extend(
            message
            for message, message_id in zip(messages, ids, strict=False)
            if message_id > candidate_end_id
        )
        cutoff_index = candidate_cutoff
        if force or estimate_prompt_tokens(system, candidate_projection, tools, chars_per_token) <= input_limit:
            break
    if cutoff_index is None and force and previous is not None:
        end_message_id = previous.end_message_id
        covered_messages: list[dict[str, Any]] = [_summary_message(previous.summary)]
    elif cutoff_index is not None:
        end_message_id = new_records[cutoff_index][1]
        covered_messages = []
        if previous is not None:
            covered_messages.append(_summary_message(previous.summary))
        covered_messages.extend(message for message, _message_id in new_records[: cutoff_index + 1])
    else:
        await _emit_budget_event(
            session,
            capability,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
            compacted=False,
            forced=force,
            reason="no_safe_boundary",
        )
        return current_history

    covered_messages, stale_reads = invalidate_stale_reads(covered_messages, session)
    previous_structured = previous.structured if previous is not None else None
    structured, summary, summary_provider, summary_model, summary_error = await _summarize(
        covered_messages,
        runtime=runtime,
        session=session,
        chars_per_token=chars_per_token,
        previous=previous_structured,
    )
    first_positive_id = next((message_id for message_id in ids if message_id > 0), end_message_id)
    start_message_id = previous.start_message_id if previous is not None else first_positive_id
    projected = [_summary_message(summary)]
    projected.extend(
        message
        for message, message_id in zip(messages, ids, strict=False)
        if message_id > end_message_id
    )
    after_tokens = estimate_prompt_tokens(system, projected, tools, chars_per_token)
    entry = CompactionEntry(
        compaction_id=None,
        session_id=session.session_id,
        schema_version=COMPACTION_SCHEMA_VERSION,
        start_message_id=start_message_id,
        end_message_id=end_message_id,
        summary=summary,
        structured=structured,
        provider=summary_provider,
        model=summary_model,
        prompt_version=COMPACTION_PROMPT_VERSION,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        context_window=context_window,
        created_at=runtime.clock.now() if getattr(runtime, "clock", None) is not None else datetime.now(UTC),
    )
    stored = session.append_compaction(entry)
    await _emit_budget_event(
        session,
        capability,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        compacted=True,
        forced=force,
        reason="provider_overflow" if force else "preflight",
        compaction_id=stored.compaction_id,
        summary_mode="fallback" if summary_error else "model",
        summary_error=summary_error,
        stale_reads=stale_reads,
    )
    return projected



STALE_READ_NOTE = (
    "[stale: {path} was modified after this read, so the content shown here is no longer current. "
    "Re-read the file if its contents matter.]"
)
DELETED_READ_NOTE = (
    "[stale: {path} no longer exists, so the content shown here is no longer current.]"
)


def invalidate_stale_reads(
    messages: list[dict[str, Any]],
    session: AgentSession,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Strip file content the summary would otherwise assert as current.

    Compaction treats `read_file` output as fact. If the file changed after that
    read, the summary bakes in outdated content stated as present-tense truth,
    and the model has no way to tell — it never sees the raw message again.
    `base_hash` does not help: it fires when an edit is applied, not when history
    is summarized.

    The comparison is against the hash recorded *at that specific read*, not
    against the session's rolling `read_files` record. That distinction is the
    whole point: `read_files` is updated on write as well as on read, so after
    the agent edits a file its rolling entry already matches disk, and comparing
    against it would miss the agent's own edits — which is the common case this
    exists to catch, not the rare external one.
    """
    workspace = Path(session.workspace)
    updated: list[dict[str, Any]] = []
    stale: list[str] = []
    for message in messages:
        read = (message.get(MESSAGE_META_KEY) or {}).get("read") if isinstance(message, dict) else None
        if not isinstance(read, dict):
            updated.append(message)
            continue
        path = str(read.get("path") or "")
        recorded = str(read.get("hash") or "")
        if not path or not recorded:
            updated.append(message)
            continue
        note = _stale_note(workspace, path, recorded)
        if note is None:
            updated.append(message)
            continue
        stale.append(path)
        replacement = dict(message)
        replacement["content"] = note
        updated.append(replacement)
    return updated, stale


def _stale_note(workspace: Path, path: str, recorded_hash: str) -> str | None:
    target = workspace / path
    try:
        if not target.is_file():
            return DELETED_READ_NOTE.format(path=path)
        current = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        # Unreadable now but readable when it was read: treat as changed rather
        # than as unchanged. Guessing "unchanged" is the one answer that lets a
        # wrong fact into the summary.
        return STALE_READ_NOTE.format(path=path)
    return None if current == recorded_hash else STALE_READ_NOTE.format(path=path)


async def _summarize(
    messages: list[dict[str, Any]],
    *,
    runtime: Any,
    session: AgentSession,
    chars_per_token: float,
    previous: StructuredSummary | None = None,
) -> tuple[StructuredSummary | None, str, str, str, str | None]:
    router = getattr(runtime, "model_runtime", None)
    if router is None:
        return None, _deterministic_summary(messages), "builtin", "deterministic", "model_router_unavailable"

    capability = router.capability_for_purpose("summarizer")
    context_settings = router.settings.context
    output_tokens = min(1_200, capability.max_output_tokens)
    input_tokens = max(1, capability.context_window - output_tokens - context_settings.reserve_tokens)
    max_chars = max(1_000, int(input_tokens * chars_per_token))
    serialized = json.dumps(messages, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(serialized) > max_chars:
        marker = "\n[middle of compaction input omitted to fit the summarizer context window]\n"
        head_chars = int((max_chars - len(marker)) * 0.6)
        tail_chars = max_chars - len(marker) - head_chars
        serialized = serialized[:head_chars] + marker + serialized[-tail_chars:]
    try:
        result = await router.stream_complete(
            purpose="summarizer",
            system=COMPACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": serialized}],
            tools=[],
            max_tokens=output_tokens,
            temperature=0.0,
        )
    except ProviderError as exc:
        return None, _deterministic_summary(messages), "builtin", "deterministic", exc.__class__.__name__
    parsed = parse_structured_summary(result.text)
    if parsed is None:
        # The reply was not a usable object. Degrade to the deterministic summary
        # rather than storing free text under a structured schema version, which
        # would make the next merge silently a no-op.
        return None, _deterministic_summary(messages), "builtin", "deterministic", "invalid_structure"
    usage = {
        "model": result.model,
        "provider": result.provider,
        "purpose": "summarizer",
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "estimated_cost": result.estimated_cost,
    }
    audit = getattr(runtime, "trace", None)
    if audit is not None:
        audit.record("usage.recorded", session_id=session.session_id, workspace=session.workspace, data=usage)
    await session.events.put({"type": "usage.recorded", **usage})
    structured = merge_summaries(previous, parsed)
    return structured, structured.render()[:COMPACTION_SUMMARY_MAX_CHARS], result.provider, result.model, None


def _deterministic_summary(messages: list[dict[str, Any]]) -> str:
    items: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = " ".join(str(message.get("content") or "").split())
        if role == "assistant" and message.get("tool_calls"):
            calls = ", ".join(str(call.get("name") or "unknown") for call in message["tool_calls"] if isinstance(call, dict))
            items.append(f"- Tool calls: {calls}")
        if not content:
            continue
        label = {"user": "User/constraints", "assistant": "Assistant progress", "tool": "Tool result"}.get(role, role)
        items.append(f"- {label}: {content[:600]}")
    if not items:
        return "- History was compacted; no textual facts could be extracted."
    return "\n".join(items)[-COMPACTION_SUMMARY_MAX_CHARS:]


def _message_groups(messages: list[dict[str, Any]]) -> list[MessageGroup]:
    groups: list[MessageGroup] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            expected = {
                str(call.get("id"))
                for call in message.get("tool_calls") or []
                if isinstance(call, dict) and call.get("id")
            }
            seen: set[str] = set()
            end = index
            cursor = index + 1
            while cursor < len(messages) and messages[cursor].get("role") == "tool":
                seen.add(str(messages[cursor].get("tool_call_id") or ""))
                end = cursor
                cursor += 1
            groups.append(MessageGroup(start=index, end=end, complete=bool(expected) and expected.issubset(seen)))
            index = end + 1
            continue
        groups.append(MessageGroup(start=index, end=index, complete=message.get("role") != "tool"))
        index += 1
    return groups


async def _emit_budget_event(
    session: AgentSession,
    capability: ModelCapability,
    *,
    before_tokens: int,
    after_tokens: int,
    compacted: bool,
    forced: bool,
    reason: str,
    compaction_id: int | None = None,
    summary_mode: str | None = None,
    summary_error: str | None = None,
    stale_reads: list[str] | None = None,
    folded_tool_outputs: int = 0,
) -> None:
    event: dict[str, Any] = {
        "type": "context.budget",
        "purpose": "history",
        "compacted": compacted,
        "forced": forced,
        "reason": reason,
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "provider": capability.provider,
        "model": capability.model,
        "context_window": capability.context_window,
    }
    if compaction_id is not None:
        event["compaction_id"] = compaction_id
    if summary_mode is not None:
        event["summary_mode"] = summary_mode
    if summary_error is not None:
        event["summary_error"] = summary_error
    if folded_tool_outputs:
        event["folded_tool_outputs"] = folded_tool_outputs
    if stale_reads:
        # Surfaced because it changes what the summary can be trusted to say:
        # these files' contents were dropped from it rather than summarized.
        event["stale_reads"] = stale_reads
    await session.events.put(event)


def _capability(
    runtime: Any,
    purpose: str,
    max_tokens: int,
    *,
    model: str | None = None,
) -> ModelCapability:
    router = getattr(runtime, "model_runtime", None)
    if model and router is not None and hasattr(router, "capability_for_model"):
        return router.capability_for_model(model)
    if router is not None and hasattr(router, "capability_for_purpose"):
        return router.capability_for_purpose(purpose)
    return ModelCapability(
        provider="unknown",
        model=purpose,
        context_window=HISTORY_TOKEN_BUDGET + max_tokens + 1_024,
        max_output_tokens=max_tokens,
        source="legacy",
    )


def _normalized_messages(session: AgentSession) -> tuple[list[dict[str, Any]], list[int]]:
    history: list[dict[str, Any]] = []
    ids: list[int] = []
    normalized_ids = _normalized_message_ids(session)
    for index, raw in enumerate(session.messages):
        if not isinstance(raw, dict):
            continue
        if "role" in raw:
            # Provenance is deliberately *not* stripped here: compaction reads it
            # to decide which file content has gone stale, and this function feeds
            # compaction as well as the provider. `strip_message_meta` removes it
            # at the outward boundary instead.
            history.append(redact_known_environment_secrets(dict(raw)))
        elif "message" in raw:
            history.append(
                {
                    "role": "user",
                    "content": redact_known_environment_secrets(str(raw["message"])),
                }
            )
        else:
            continue
        ids.append(normalized_ids[index] if index < len(normalized_ids) else 0)
    return history, ids


def _normalized_message_ids(session: AgentSession) -> list[int]:
    if len(session.message_ids) == len(session.messages):
        return list(session.message_ids)
    return [0] * len(session.messages)


def _summary_message(summary: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": f"[persistent history summary · schema v{COMPACTION_SCHEMA_VERSION}]\n{summary}",
    }
