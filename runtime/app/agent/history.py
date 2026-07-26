from __future__ import annotations

import json
from typing import Any

from app.security.secrets import redact_known_environment_secrets
from app.sessions.store import Session

HISTORY_TOKEN_BUDGET = 60_000
HARD_BUDGET_FACTOR = 1.5
KEEP_RECENT_MESSAGES = 8

TOOL_OUTPUT_LIMITS = {"bash": 8_000, "run_tests": 8_000, "read_file": 0, "default": 6_000}


def load_history(session: Session) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for raw in session.messages:
        if not isinstance(raw, dict):
            continue
        if "role" in raw:
            history.append(redact_known_environment_secrets(dict(raw)))
        elif "message" in raw:
            history.append(
                {
                    "role": "user",
                    "content": redact_known_environment_secrets(str(raw["message"])),
                }
            )
    return history


def persist_message(session: Session, message: dict[str, Any]) -> None:
    session.append_message(message)


def truncate_tool_output(tool_name: str, text: str) -> str:
    limit = TOOL_OUTPUT_LIMITS.get(tool_name, TOOL_OUTPUT_LIMITS["default"])
    if limit <= 0 or len(text) <= limit:
        return text
    marker = f"\n[输出已截断: {len(text) - limit} 字符省略，可用 offset/分页参数继续查看]\n"
    head = int(limit * 0.65)
    tail = limit - head
    return text[:head] + marker + text[-tail:]


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    return int(len(json.dumps(messages, ensure_ascii=False, default=str)) / 3.5)


async def compact_if_needed(history: list[dict[str, Any]], runtime: Any, session: Session) -> list[dict[str, Any]]:
    before = estimate_tokens(history)
    if before <= HISTORY_TOKEN_BUDGET:
        return history

    compacted = [dict(message) for message in history]
    cutoff = max(0, len(compacted) - KEEP_RECENT_MESSAGES)
    for index in range(cutoff):
        if estimate_tokens(compacted) <= HISTORY_TOKEN_BUDGET:
            break
        message = compacted[index]
        if message.get("role") != "tool" or str(message.get("content") or "").startswith("[工具输出已压缩"):
            continue
        original_chars = len(str(message.get("content") or ""))
        message["content"] = f"[工具输出已压缩: {original_chars} 字符，如需内容请重新调用工具]"

    if estimate_tokens(compacted) > HISTORY_TOKEN_BUDGET * HARD_BUDGET_FACTOR and runtime.model_router is not None:
        compacted = await summarize_history_head(compacted, runtime)

    after = estimate_tokens(compacted)
    await session.events.put(
        {
            "type": "context.budget",
            "purpose": "history",
            "compacted": True,
            "before_tokens": before,
            "after_tokens": after,
        }
    )
    return compacted


async def summarize_history_head(history: list[dict[str, Any]], runtime: Any) -> list[dict[str, Any]]:
    half = len(history) // 2
    head, tail = history[:half], history[half:]
    result = await runtime.model_router.stream_complete(
        purpose="summarizer",
        system="把以下 agent 对话压缩为要点：用户目标、已完成的探索/修改、关键发现、未完成事项。只输出要点列表。",
        messages=[{"role": "user", "content": json.dumps(head, ensure_ascii=False, default=str)[:40_000]}],
        max_tokens=800,
    )
    summary = {"role": "user", "content": f"[历史摘要] {result.text}"}
    return [summary, *tail]
