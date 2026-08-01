"""Embedding SDK: stdio JSONL RPC over the same Application Runtime the HTTP server uses."""

from app.sdk.client import AgentClient
from app.sdk.protocol import MIN_PROTOCOL_VERSION, PROTOCOL_VERSION, RpcError
from app.sdk.server import StdioServer, serve_stdio

__all__ = [
    "MIN_PROTOCOL_VERSION",
    "PROTOCOL_VERSION",
    "AgentClient",
    "RpcError",
    "StdioServer",
    "serve_stdio",
]
