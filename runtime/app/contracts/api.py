from __future__ import annotations

from typing import Any


API_CONTRACT_VERSION = "2.0"
API_MIN_SUPPORTED_VERSION = "2.0"


def contract_descriptor(runtime_version: str) -> dict[str, Any]:
    """Versioned contract shared by CLI, HTTP/SSE and future transports."""

    return {
        "contract_version": API_CONTRACT_VERSION,
        "min_supported_version": API_MIN_SUPPORTED_VERSION,
        "runtime_version": runtime_version,
        "transports": {
            "http": {"version": "v1", "status": "stable"},
            "sse": {"event_schema": "v2", "status": "stable"},
            "stdio_jsonrpc": {"version": "2.0", "status": "planned"},
        },
    }
