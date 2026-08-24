"""Wire format for the stdio JSONL RPC.

One JSON object per line, in both directions. Chosen over a framed protocol
because the transport is a pipe an embedder already has: a line is a message, so
a host can read it with `readline` in any language without a length-prefix
parser.

Three shapes travel over it:

- request   {"id": "1", "method": "session.create", "params": {...}}
- response  {"id": "1", "result": {...}} | {"id": "1", "error": {...}}
- notify    {"method": "event", "params": {...}}   (no id — nothing to reply to)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Bumped when a change would break an existing embedder. The server accepts a
# range rather than one value so a host does not have to upgrade in lockstep
# with the Runtime.
PROTOCOL_VERSION = 1
MIN_PROTOCOL_VERSION = 1

# Error codes are strings rather than integers: an embedder reads them in logs
# and in `except` branches, and `"unknown_method"` needs no lookup table.
ERR_PARSE = "parse_error"
ERR_INVALID_REQUEST = "invalid_request"
ERR_UNKNOWN_METHOD = "unknown_method"
ERR_NOT_INITIALIZED = "not_initialized"
ERR_UNSUPPORTED_VERSION = "unsupported_protocol_version"
ERR_NOT_FOUND = "not_found"
ERR_INTERNAL = "internal_error"


class RpcError(Exception):
    """An error the host is meant to read, not a crash."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            payload["data"] = self.data
        return payload


@dataclass(slots=True)
class Request:
    id: Any
    method: str
    params: dict[str, Any]

    @classmethod
    def parse(cls, payload: Any) -> Request:
        if not isinstance(payload, dict):
            raise RpcError(ERR_INVALID_REQUEST, "request must be a JSON object")
        method = payload.get("method")
        if not isinstance(method, str) or not method:
            raise RpcError(ERR_INVALID_REQUEST, "request must include a non-empty string method")
        params = payload.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RpcError(ERR_INVALID_REQUEST, "params must be a JSON object")
        # A request without an id is a notification: it is executed, but its
        # result is discarded rather than being written back with `"id": null`,
        # which a host would have no way to correlate.
        return cls(id=payload.get("id"), method=method, params=params)


def response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"id": request_id, "result": result}


def error_response(request_id: Any, error: RpcError) -> dict[str, Any]:
    return {"id": request_id, "error": error.to_dict()}


def notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"method": method, "params": params}


def negotiate(requested: Any) -> int:
    """Agree a protocol version, or refuse with the range we do support.

    Refuses rather than silently downgrading: a host that asked for a version
    this Runtime cannot speak has features in mind that would fail later, in
    ways much harder to attribute than a rejected handshake.
    """
    if requested is None:
        return PROTOCOL_VERSION
    if not isinstance(requested, int) or isinstance(requested, bool):
        raise RpcError(ERR_INVALID_REQUEST, "protocol_version must be an integer")
    if requested < MIN_PROTOCOL_VERSION or requested > PROTOCOL_VERSION:
        raise RpcError(
            ERR_UNSUPPORTED_VERSION,
            f"protocol version {requested} is not supported",
            {"supported_min": MIN_PROTOCOL_VERSION, "supported_max": PROTOCOL_VERSION},
        )
    return requested
