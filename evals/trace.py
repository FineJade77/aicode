from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from typing import Any

from app.agent.history import COMPACTION_PROMPT_VERSION
from app.audit.redaction import redact
from evals import EVAL_CONTRACT_VERSION, RUNNER_VERSION


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_tool_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool == "bash":
        command = str(arguments.get("command") or "")
        existing_hash = str(arguments.get("command_hash") or "")
        try:
            parts = shlex.split(command)
            executable = Path(parts[0]).name if parts else ""
        except ValueError:
            executable = ""
        return {
            "command_sha256": existing_hash or sha256_text(command),
            "command_chars": len(command),
            "executable": executable,
            "timeout": arguments.get("timeout"),
        }
    if tool == "edit_file":
        old_text = str(arguments.get("old_text") or "")
        new_text = str(arguments.get("new_text") or "")
        return {
            "path": str(arguments.get("path") or ""),
            "delete": bool(arguments.get("delete")),
            "old_text_sha256": sha256_text(old_text),
            "old_text_chars": len(old_text),
            "new_text_sha256": sha256_text(new_text),
            "new_text_chars": len(new_text),
        }
    if tool in {"read_file", "related_files"}:
        return {
            key: arguments.get(key)
            for key in ("path", "workspace", "offset", "limit")
            if key in arguments
        }
    if tool == "list_files":
        return {
            key: arguments.get(key)
            for key in ("path", "workspace", "max_depth")
            if key in arguments
        }
    if tool == "search":
        query = str(arguments.get("query") or "")
        return {
            "query_sha256": sha256_text(query),
            "query_chars": len(query),
            "glob": arguments.get("glob"),
            "limit": arguments.get("limit"),
            "workspace": arguments.get("workspace"),
        }
    return {}


def safe_model_call(call: dict[str, Any]) -> dict[str, Any]:
    response = call.get("response") or {}
    return {
        "call_index": call.get("call_index"),
        "purpose": call.get("purpose"),
        "model": call.get("model"),
        "message_count": call.get("message_count"),
        "input_estimate": call.get("input_estimate"),
        "offered_tools": call.get("offered_tools") or [],
        "max_tokens": call.get("max_tokens"),
        "status": call.get("status"),
        "response": {
            "text_sha256": sha256_text(str(response.get("text") or "")),
            "text_chars": len(str(response.get("text") or "")),
            "tool_calls": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "arguments": safe_tool_arguments(str(item.get("name") or ""), item.get("arguments") or {}),
                }
                for item in response.get("tool_calls") or []
            ],
            "input_tokens": response.get("input_tokens", 0),
            "output_tokens": response.get("output_tokens", 0),
        },
        "error_type": call.get("error_type"),
    }


def sanitize_session_event(event: dict[str, Any], literals: list[str]) -> dict[str, Any]:
    sanitized = dict(event)
    event_type = str(sanitized.get("type") or "")
    if event_type == "tool.started":
        sanitized["args"] = safe_tool_arguments(
            str(sanitized.get("tool") or ""),
            sanitized.get("args") or {},
        )
    if event_type == "approval.requested":
        if "args" in sanitized:
            sanitized["args"] = safe_tool_arguments(
                str(sanitized.get("tool") or ""),
                sanitized.get("args") or {},
            )
        if "diff" in sanitized:
            diff = str(sanitized.pop("diff") or "")
            sanitized["diff_sha256"] = sha256_text(diff)
            sanitized["diff_chars"] = len(diff)
    for key in ("text", "summary"):
        if key in sanitized:
            value = str(sanitized.pop(key) or "")
            sanitized[f"{key}_sha256"] = sha256_text(value)
            sanitized[f"{key}_chars"] = len(value)
    if event_type == "tool.output":
        sanitized.pop("data", None)
    return redact_literals(redact(sanitized), literals)


def sanitize_audit_event(event: dict[str, Any], literals: list[str]) -> dict[str, Any]:
    sanitized = dict(event)
    sanitized["workspace"] = "."
    data = dict(sanitized.get("data") or {})
    if sanitized.get("event_type") == "tool.started":
        data["args"] = safe_tool_arguments(str(data.get("tool") or ""), data.get("args") or {})
    sanitized["data"] = data
    return redact_literals(redact(sanitized), literals)


def redact_literals(value: Any, literals: list[str]) -> Any:
    if isinstance(value, dict):
        return {key: redact_literals(child, literals) for key, child in value.items()}
    if isinstance(value, list):
        return [redact_literals(child, literals) for child in value]
    if isinstance(value, tuple):
        return [redact_literals(child, literals) for child in value]
    if isinstance(value, str):
        for literal in sorted((item for item in literals if item), key=len, reverse=True):
            value = value.replace(literal, "[REDACTED]")
    return value


def source_versions(repository_root: Path) -> dict[str, str]:
    harness_files = (
        "evals/contracts.py",
        "evals/provider.py",
        "evals/trace.py",
        "evals/graders/deterministic.py",
        "evals/runner/core.py",
    )
    harness_digest = hashlib.sha256()
    for relative in harness_files:
        harness_digest.update(relative.encode("utf-8"))
        harness_digest.update(b"\0")
        harness_digest.update((repository_root / relative).read_bytes())
        harness_digest.update(b"\0")
    eval_schema_digest = hashlib.sha256()
    for relative in (
        "schemas/eval-task.schema.json",
        "schemas/eval-trace.schema.json",
        "schemas/eval-report.schema.json",
    ):
        eval_schema_digest.update(relative.encode("utf-8"))
        eval_schema_digest.update(b"\0")
        eval_schema_digest.update((repository_root / relative).read_bytes())
        eval_schema_digest.update(b"\0")
    return {
        "aicode_version": (repository_root / "VERSION").read_text(encoding="utf-8").strip(),
        "eval_contract": EVAL_CONTRACT_VERSION,
        "eval_runner": RUNNER_VERSION,
        "eval_harness_sha256": harness_digest.hexdigest(),
        "eval_schema_sha256": eval_schema_digest.hexdigest(),
        "prompt_sha256": sha256_file(repository_root / "runtime/app/agent/prompts.py"),
        "tool_schema_sha256": sha256_file(repository_root / "schemas/tools.schema.json"),
        "policy_sha256": sha256_file(repository_root / "runtime/app/agent/policy.py"),
        "compaction_prompt_version": COMPACTION_PROMPT_VERSION,
    }


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(encoded)
