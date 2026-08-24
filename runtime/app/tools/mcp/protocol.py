from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "aicode", "version": "1"}
# Only tool use is requested. aicode does not consume prompts or resources, and
# asking for capabilities it will not use would widen the trust surface for no
# benefit.
CLIENT_CAPABILITIES: dict[str, Any] = {"tools": {}}


class McpProtocolError(RuntimeError):
    """The server spoke something other than the MCP subset aicode understands."""


def encode(message: dict[str, Any]) -> bytes:
    """Frame one JSON-RPC message as a single newline-terminated line."""
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any]:
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise McpProtocolError(f"server sent a line that is not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise McpProtocolError("server sent a JSON value that is not an object")
    return message


def request(request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def result_of(message: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a JSON-RPC response, turning an error payload into an exception."""
    if "error" in message:
        error = message["error"] or {}
        code = error.get("code", "?") if isinstance(error, dict) else "?"
        text = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        raise McpProtocolError(f"server returned error {code}: {text}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpProtocolError("server response is missing a result object")
    return result


def tool_result_text(result: dict[str, Any]) -> tuple[str, bool]:
    """Flatten an MCP tool result into text plus an error flag.

    MCP returns a list of typed content blocks. Non-text blocks are summarised by
    type rather than dropped, so a caller can see that something came back even
    when aicode cannot render it.
    """
    is_error = bool(result.get("isError"))
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return ("", is_error)
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        else:
            parts.append(f"[{block.get('type') or 'unknown'} content omitted]")
    return ("\n".join(part for part in parts if part), is_error)
