"""A minimal MCP server over stdio, used to test the client end to end.

Run as a subprocess by the tests. Behaviour is switched by argv so one script
covers the healthy path and each failure mode:

    healthy   normal handshake, tools/list and tools/call
    crash     exits during the handshake
    hang      accepts the handshake then never answers a tool call
    garbage   emits a line that is not JSON
    error     answers tools/call with isError
"""

from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the supplied text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "shout",
        # No description, to prove the client synthesises one.
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "healthy"
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        message = json.loads(raw)
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            if mode == "crash":
                sys.stderr.write("fake server refuses to start\n")
                sys.stderr.flush()
                raise SystemExit(3)
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            if mode == "garbage":
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
                continue
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            if mode == "hang":
                time.sleep(30)
                continue
            params = message.get("params") or {}
            text = str((params.get("arguments") or {}).get("text", ""))
            if mode == "error":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"content": [{"type": "text", "text": "tool failed"}], "isError": True},
                    }
                )
                continue
            payload = text.upper() if params.get("name") == "shout" else text
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": payload},
                            {"type": "image", "data": "ignored"},
                        ]
                    },
                }
            )
        elif request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()
